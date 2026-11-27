"""Native snapshot requests and Home Assistant image lifecycle, without a camera."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.camera import async_get_image, get_camera_from_entity_id
from homeassistant.exceptions import HomeAssistantError

from custom_components.myq.api.client import Client
from custom_components.myq.api.errors import (
    AccessDenied,
    CannotConnect,
    ProtocolError,
    RateLimited,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.video import VIDEO, Video
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import Snapshot

from .ha import MockConfigEntry

JPEG = b"\xff\xd8\xffsynthetic-image\xff\xd9"


async def test_snapshot_refresh_wire_contract(http, tokens, camera_device):
    http.request.return_value = Response(200, {"Content-Type": "image/jpeg"}, JPEG)
    assert await Video(Client(transport=http, tokens=tokens)).snapshot(camera_device) == JPEG
    http.request.assert_awaited_once_with(
        "GET",
        VIDEO + "/cxs/api/devices/CAM123/cam/recentImage.jpg",
        headers={"AntiCSRF": "AntiCSRF", "Authorization": "Bearer test-access"},
        params={"size": "HD", "forceRefresh": "true"},
        timeout=6,
    )


def recent():
    # tend-dttm: the capture time CameraSnapshotProvider records for each image.
    return {"tend-dttm": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")}


def battery(level, **settings):
    return {
        "status": {"online": True, "pwr": {"battLv": level}},
        "settings": settings,
        "cap": {"pwr": {"powerSaverAutoBattLvThresh": 40}},
    }


async def test_battery_camera_reuses_the_existing_image(http, tokens, camera_device):
    # CameraSnapshotProvider: a valid battLv marks a battery camera.
    http.request.return_value = Response(200, recent(), JPEG)
    video = Video(Client(transport=http, tokens=tokens))
    assert await video.snapshot(camera_device, battery(80)) == JPEG
    http.request.assert_awaited_once()
    assert http.request.call_args.kwargs["params"] == {"size": "HD", "forceUseExisting": "true"}
    assert video.captured[camera_device.key] is not None


@pytest.mark.parametrize(
    "headers,level,refreshes",
    [
        ({}, 80, True),  # No capture time.
        ({"tend-dttm": "2020-01-01T00:00:00"}, 80, True),  # Older than 24 hours.
        ({"tend-dttm": "2020-01-01T00:00:00"}, 8, False),  # Battery too low to refresh.
    ],
)
async def test_stale_battery_image_is_refreshed_while_battery_is_healthy(
    http, tokens, camera_device, headers, level, refreshes
):
    fresh = b"\xff\xd8\xfffresh"
    http.request.side_effect = [Response(200, headers, JPEG), Response(200, recent(), fresh)]
    video = Video(Client(transport=http, tokens=tokens))
    assert await video.snapshot(camera_device, battery(level)) == (fresh if refreshes else JPEG)
    assert http.request.await_count == (2 if refreshes else 1)


async def test_failed_stale_refresh_keeps_the_existing_image(http, tokens, camera_device):
    http.request.side_effect = [Response(200, {}, JPEG), CannotConnect("timeout")]
    video = Video(Client(transport=http, tokens=tokens))
    assert await video.snapshot(camera_device, battery(80)) == JPEG


@pytest.mark.parametrize("error", [CannotConnect("timeout"), ProtocolError("bad")])
async def test_any_load_failure_falls_back(http, tokens, camera_device, error):
    http.request.side_effect = [error, Response(200, {}, JPEG)]
    video = Video(Client(transport=http, tokens=tokens))
    assert await video.snapshot(camera_device, {"status": {"online": True}}) == JPEG


async def test_authentication_failure_is_not_hidden_by_fallback(http, tokens, camera_device):
    http.request.side_effect = [RateLimited(60), Response(200, {}, JPEG)]
    video = Video(Client(transport=http, tokens=tokens))
    with pytest.raises(RateLimited):
        await video.snapshot(camera_device, {"status": {"online": True}})
    http.request.assert_awaited_once()


@pytest.mark.parametrize(
    "details,refreshes",
    [
        (battery(80), True),
        (battery(8), False),  # CRITICAL_LOW band.
        (battery(80, wakeOnMotion=False), False),  # Battery saver (wake on motion off).
        # The snapshot rule reads only wake on motion, not the power saver mode.
        (battery(30, pwr={"powerSaverMode": "ON"}), True),
    ],
)
async def test_battery_camera_refreshes_only_when_reuse_fails_and_battery_is_healthy(
    http, tokens, camera_device, details, refreshes
):
    http.request.side_effect = [Response(404, {}, b""), Response(200, {}, JPEG)]
    video = Video(Client(transport=http, tokens=tokens))
    if refreshes:
        assert await video.snapshot(camera_device, details) == JPEG
        assert http.request.call_args.kwargs["params"] == {"size": "HD", "forceRefresh": "true"}
    else:
        with pytest.raises(ProtocolError):
            await video.snapshot(camera_device, details)
        http.request.assert_awaited_once()


async def test_wired_camera_falls_back_to_the_existing_image(http, tokens, camera_device):
    http.request.side_effect = [Response(200, {}, b"<html>"), Response(200, {}, JPEG)]
    video = Video(Client(transport=http, tokens=tokens))
    assert await video.snapshot(camera_device, {"status": {"online": True}}) == JPEG
    assert http.request.call_args.kwargs["params"] == {"size": "HD", "forceUseExisting": "true"}


@pytest.mark.parametrize("body", [b"", b"<html>sign in</html>", b'{"error":"unavailable"}'])
async def test_snapshot_rejects_non_image_response(http, tokens, camera_device, body):
    http.request.return_value = Response(200, {}, body)
    with pytest.raises(ProtocolError, match="JPEG"):
        await Video(Client(transport=http, tokens=tokens)).snapshot(camera_device)


@pytest.mark.parametrize(
    "device_type",
    ["SmartGarageLCCam2", "smartgaragelcanzcam2", "SmartIndoorLCCam2", "videokeypadcocam"],
)
def test_newer_camera_discovery_without_family(camera_device, device_type):
    assert replace(camera_device, family="", device_type=device_type).is_camera


def test_opener_camera_reference_does_not_create_camera(door):
    referenced = replace(door, state={**door.state, "camera_serial_number": "TEST-CAM123"})
    assert not referenced.is_camera
    assert not replace(door, family="", device_type="smartlock").is_camera


@pytest.fixture
async def camera_entity(hass, enable_custom_integrations, tokens, camera_device, camera_details):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-camera-user",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={camera_device.key: camera_device}),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(return_value=camera_details),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = hass.states.async_all("camera")[0].entity_id
    entity = get_camera_from_entity_id(hass, entity_id)
    yield entity
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_ha_image_uses_refresh_and_coalesces_requests(hass, camera_entity):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(device, details=None):
        entered.set()
        await release.wait()
        return JPEG

    with patch.object(
        camera_entity.coordinator.video, "snapshot", AsyncMock(side_effect=fetch)
    ) as get:
        tasks = [
            asyncio.create_task(async_get_image(hass, camera_entity.entity_id)) for _ in range(4)
        ]
        await entered.wait()
        release.set()
        images = await asyncio.gather(*tasks)
        assert all(image.content == JPEG and image.content_type == "image/jpeg" for image in images)
        get.assert_awaited_once()
        camera_entity._image_time -= 61
        assert (await async_get_image(hass, camera_entity.entity_id)).content == JPEG
        assert get.await_count == 2


@pytest.mark.parametrize(
    "transition", ["off", "offline", "removed", "tend_offline", "privacy_unknown"]
)
async def test_in_flight_snapshot_is_discarded_when_camera_becomes_unavailable(
    hass, camera_entity, camera_device, camera_details, transition
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(device, details=None):
        entered.set()
        await release.wait()
        return JPEG

    with patch.object(camera_entity.coordinator.video, "snapshot", AsyncMock(side_effect=fetch)):
        pending = asyncio.create_task(camera_entity.async_camera_image())
        await entered.wait()
        devices = {camera_device.key: camera_device}
        if transition == "off":
            camera_details["settings"]["cam"]["enabled"] = False
        elif transition == "offline":
            devices[camera_device.key] = replace(camera_device, state={"online": False})
        elif transition == "tend_offline":
            camera_details["status"]["online"] = False
        elif transition == "privacy_unknown":
            camera_details["settings"]["cam"]["enabled"] = "maybe"
        else:
            devices.clear()
        camera_entity.coordinator.async_set_updated_data(
            Snapshot(devices, {camera_device.key: camera_details})
        )
        await hass.async_block_till_done()
        release.set()
        assert await pending is None
        assert camera_entity._image is None
        assert await camera_entity.async_camera_image() is None


async def test_failed_fresh_snapshot_does_not_return_expired_image(camera_entity):
    camera_entity._image = JPEG
    camera_entity._image_time = 0
    with patch.object(
        camera_entity.coordinator.video, "snapshot", AsyncMock(side_effect=AccessDenied("Denied"))
    ):
        with pytest.raises(HomeAssistantError, match="Denied"):
            await camera_entity.async_camera_image()
    assert camera_entity._image is None


def test_missing_privacy_flag_means_the_camera_is_on(camera_entity):
    # CameraSettings.enabled defaults to true; privacy needs enableSwitch && !enabled.
    camera_entity.details["settings"]["cam"].pop("enabled")
    assert camera_entity.is_on
    camera_entity.details["settings"]["cam"]["enabled"] = False
    camera_entity.details["cap"]["cam"]["enableSwitch"] = False
    assert camera_entity.is_on
    camera_entity.details["cap"]["cam"]["enableSwitch"] = True
    assert not camera_entity.is_on


@pytest.mark.parametrize("enabled", ["true", "false", 0, 1, {}, []])
async def test_malformed_privacy_state_never_exposes_cached_image(camera_entity, enabled):
    camera_entity._image = JPEG
    camera_entity.details["settings"]["cam"]["enabled"] = enabled
    with patch.object(camera_entity.coordinator.video, "snapshot", AsyncMock()) as fetch:
        assert not camera_entity.is_on
        assert await camera_entity.async_camera_image() is None
    fetch.assert_not_awaited()
    assert camera_entity._image is None


def test_sleeping_camera_reporting_offline_stays_available(camera_entity):
    # DevicesManager still wakes such a camera; the app gates images on MyQ online.
    camera_entity.details["status"] = {
        "online": False,
        "pwr": {"sleepInterval": 30, "battLv": 70},
    }
    assert camera_entity.available
    camera_entity.details["status"] = {"online": False}
    assert not camera_entity.available

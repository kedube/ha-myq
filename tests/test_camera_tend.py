"""A TC camera in Home Assistant: a stream source read by Home Assistant's own players."""

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    StreamType,
    get_camera_from_entity_id,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api import tend_source
from custom_components.myq.api.errors import AuthenticationError, CannotConnect
from custom_components.myq.camera import MyQCamera, MyQTendCamera, _live_route
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import Snapshot
from custom_components.myq.diagnostics import async_get_config_entry_diagnostics

START = b"\x00\x00\x00\x01"
KEYFRAME = START + b"\x67sps" + START + b"\x68pps" + START + b"\x65\x88picture"


class FakeTendSession:
    """Only the camera session is replaced; Home Assistant and the entity are real."""

    instances: list["FakeTendSession"] = []
    start_error: Exception | None = None

    def __init__(self, openers=()):
        self.openers = set(openers)
        self.on_video = self.on_status = None
        self.media = None
        self.transport = {"video_mode": None, "modes_tried": []}
        self.ended = asyncio.get_running_loop().create_future()
        self.closed = False
        self.start = AsyncMock(side_effect=self._start)
        type(self).instances.append(self)

    async def _start(self, alias, token):
        if type(self).start_error is not None:
            raise type(self).start_error

    async def wait_closed(self):
        await asyncio.shield(self.ended)

    async def close(self):
        self.closed = True
        if not self.ended.done():
            self.ended.set_result(None)


@pytest.fixture
async def tend_camera(hass, enable_custom_integrations, tokens, camera_device, camera_details):
    camera_device = replace(
        camera_device,
        serial="TC-TEST-CAMERA",
        state={"online": True, "paired_gdo_serial_number": "OPENER-1"},
    )
    camera_details["alias"] = "owner:camera"
    FakeTendSession.instances, FakeTendSession.start_error = [], None
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-tend-camera-user",
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
        patch("custom_components.myq.camera.LegacyTendSession", FakeTendSession),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity = get_camera_from_entity_id(hass, hass.states.async_all("camera")[0].entity_id)
        yield SimpleNamespace(
            camera=entity,
            entry=entry,
            device=camera_device,
            details=camera_details,
            sessions=FakeTendSession.instances,
        )
        if entry.state.recoverable:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def until(condition):
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


def test_tc_serial_always_uses_the_stream_source_route(camera_device):
    tc = replace(camera_device, serial="TC-TEST-CAMERA")
    for capability in ({}, {"kvsWebRTC": False}, {"kvsWebRTC": True}, {"kvsWebRTC": "odd"}):
        assert _live_route(tc, {"alias": "owner:camera", "cap": {"cam": capability}}) == "legacy"
    assert _live_route(tc, {"cap": {"cam": {"kvsWebRTC": True}}}) is None
    assert _live_route(tc, {"alias": "x" * 257}) is None
    other = {"alias": "owner:camera", "cap": {"cam": {"kvsWebRTC": True}}}
    assert _live_route(camera_device, other) == "kvs"
    assert _live_route(camera_device, {"alias": "owner:camera", "cap": {"cam": {}}}) is None
    assert _live_route(None, {"alias": "owner:camera"}) is None


async def test_tc_camera_is_a_stream_source_not_a_native_webrtc_camera(tend_camera, socket_enabled):
    camera = tend_camera.camera
    assert type(camera) is MyQTendCamera
    # Home Assistant only uses its own players when the class leaves WebRTC to it.
    assert MyQTendCamera.async_handle_async_webrtc_offer is Camera.async_handle_async_webrtc_offer
    assert MyQCamera.async_handle_async_webrtc_offer is not Camera.async_handle_async_webrtc_offer
    assert camera.supported_features & CameraEntityFeature.STREAM
    assert camera.camera_capabilities.frontend_stream_types == {StreamType.HLS}
    assert camera.stream_options == {"use_wallclock_as_timestamps": True}
    url = await camera.stream_source()
    assert url.startswith("http://127.0.0.1:") and url.endswith(".h264")
    assert url == await camera.stream_source()
    assert not tend_camera.sessions  # Nothing contacts the camera until a player reads.


async def test_no_alias_means_no_stream(hass, tend_camera):
    camera = tend_camera.camera
    details = {**tend_camera.details, "alias": None}
    camera.coordinator.async_set_updated_data(
        Snapshot({tend_camera.device.key: tend_camera.device}, {tend_camera.device.key: details})
    )
    await hass.async_block_till_done()
    assert not camera.supported_features & CameraEntityFeature.STREAM
    assert await camera.stream_source() is None


async def test_reader_opens_the_camera_and_receives_its_video(hass, tend_camera, socket_enabled):
    camera = tend_camera.camera
    url = await camera.stream_source()
    wake = AsyncMock(return_value=tend_camera.details)
    with patch.object(camera.coordinator.video, "wake_best_effort", wake):
        async with aiohttp.ClientSession() as http, http.get(url) as response:
            assert response.status == 200
            session = tend_camera.sessions[0]
            session.start.assert_awaited_once_with("owner:camera", "test-access")
            assert session.openers == {"OPENER-1"}
            wake.assert_awaited_once()
            # The idle status connection is paused while the camera session is open.
            assert camera.coordinator._live_owners == {session}
            assert camera._live_open
            session.on_video(KEYFRAME)
            async with asyncio.timeout(2):
                assert await response.content.readexactly(len(KEYFRAME)) == KEYFRAME
            data = await async_get_config_entry_diagnostics(hass, tend_camera.entry)
            row = data["devices"][0]
            assert row["live_video_route"] == "legacy" and row["live_view_open"] is True
            assert row["live_video_source"]["session_open"] is True
            assert row["live_video_source"]["readers"] == 1
            assert row["live_video_source"]["keyframes"] == 1
            assert url.rsplit("/", 1)[1] not in str(data)
    assert len(tend_camera.sessions) == 1


async def test_session_closes_after_the_last_reader_and_releases_the_status_feed(
    tend_camera, socket_enabled, monkeypatch
):
    monkeypatch.setattr(tend_source, "IDLE_SECONDS", 0.05)
    camera = tend_camera.camera
    with patch.object(
        camera.coordinator.video, "wake_best_effort", AsyncMock(return_value=tend_camera.details)
    ):
        async with aiohttp.ClientSession() as http, http.get(await camera.stream_source()):
            await until(lambda: camera._live_open)
    await until(lambda: tend_camera.sessions[0].closed and not camera.coordinator._live_owners)
    assert not camera._live_open


@pytest.mark.parametrize("change", ["offline", "privacy", "removed"])
async def test_coordinator_ends_video_when_the_camera_may_no_longer_stream(
    hass, tend_camera, socket_enabled, change
):
    camera = tend_camera.camera
    devices = {tend_camera.device.key: tend_camera.device}
    details = dict(tend_camera.details)
    with patch.object(
        camera.coordinator.video, "wake_best_effort", AsyncMock(return_value=tend_camera.details)
    ):
        async with (
            aiohttp.ClientSession() as http,
            http.get(await camera.stream_source()) as response,
        ):
            await until(lambda: camera._live_open)
            if change == "offline":
                details["status"] = {"online": False}
            elif change == "privacy":
                details["settings"] = {"cam": {"enabled": False}}
            else:
                devices = {}
            camera.coordinator.async_set_updated_data(
                Snapshot(devices, {tend_camera.device.key: details})
            )
            async with asyncio.timeout(2):
                await response.content.read()  # The stream ends.
    await until(lambda: tend_camera.sessions[0].closed and not camera.coordinator._live_owners)


@pytest.mark.parametrize("failure", ["unavailable", "route", "start"])
async def test_failed_start_answers_the_player_and_releases_everything(
    hass, tend_camera, socket_enabled, failure, caplog
):
    camera = tend_camera.camera
    url = await camera.stream_source()
    refreshed = dict(tend_camera.details)
    if failure == "route":
        refreshed["settings"] = {"cam": {"enabled": False}}
    elif failure == "start":
        FakeTendSession.start_error = CannotConnect("Tend legacy video failed or timed out")
    else:
        camera.coordinator.async_set_updated_data(
            Snapshot(
                {tend_camera.device.key: tend_camera.device},
                {tend_camera.device.key: {**tend_camera.details, "status": {"online": False}}},
            )
        )
        await hass.async_block_till_done()
    caplog.set_level(logging.DEBUG, logger="custom_components.myq.camera")
    with patch.object(
        camera.coordinator.video, "wake_best_effort", AsyncMock(return_value=refreshed)
    ):
        async with aiohttp.ClientSession() as http:
            for _ in range(2):
                async with http.get(url) as response:
                    assert response.status == 503
                # Lift the pause that would refuse this player's retry, so the second
                # request reaches the camera check and fails the same way again.
                camera._source._retry_at = 0
    assert all(session.closed for session in tend_camera.sessions)
    assert not camera.coordinator._live_owners and not camera._live_open
    reports = [r for r in caplog.records if "live video could not start" in r.getMessage()]
    # The first failure is a warning; the same failure again is not repeated at that level.
    assert [r.levelno for r in reports] == [logging.WARNING, logging.DEBUG]
    data = await async_get_config_entry_diagnostics(hass, tend_camera.entry)
    assert data["devices"][0]["live_video_source"]["last_error"]


@pytest.mark.parametrize(
    "power,platform,expected",
    [
        (None, None, False),  # Powered and reporting available: the request is unchanged.
        ({"sleepInterval": 30, "sleepMode": "LOW_POWER_DEEP_SLEEP"}, None, True),
        ({"sleepMode": "FULL_POWER"}, "MyQ:PMX-SAV524MT7686-STM-II", True),  # Battery model.
        ({"sleepMode": "FULL_POWER_BOOT"}, None, True),  # Still waking.
    ],
)
async def test_video_request_asks_tend_to_wake_a_camera_that_may_sleep(
    hass, tend_camera, power, platform, expected
):
    camera = tend_camera.camera
    details = {**tend_camera.details, "status": {"online": True}}
    if power is not None:
        details["status"]["pwr"] = power
    if platform is not None:
        details["hw"] = {"platform": platform}
    with patch.object(camera.coordinator.video, "wake_best_effort", return_value=details):
        session = await camera._open_video(lambda record: None)
    try:
        assert (session.wakeup, session.camera_asleep) == ((1 if expected else None), expected)
        session.start.assert_awaited_once_with("owner:camera", "test-access")
    finally:
        await camera._abandon(session)


async def test_start_cancelled_twice_still_releases_the_status_feed(hass, tend_camera):
    """The reader leaves, then video is stopped, while the session is still closing."""
    camera = tend_camera.camera
    starting, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def start(alias, token):
        starting.set()
        await asyncio.Event().wait()

    close_session = FakeTendSession.close

    async def close(self):
        closing.set()
        await finish.wait()
        await close_session(self)

    with (
        patch.object(
            camera.coordinator.video, "wake_best_effort", return_value=tend_camera.details
        ),
        patch.object(FakeTendSession, "_start", side_effect=start),
        patch.object(FakeTendSession, "close", close),
    ):
        opening = asyncio.create_task(camera._open_video(lambda record: None))
        await starting.wait()
        opening.cancel()
        await closing.wait()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert camera.coordinator._live_owners == {tend_camera.sessions[0]}
        finish.set()
        await until(lambda: tend_camera.sessions[0].closed)
        # Otherwise the idle status connection would stay paused until a reload.
        await until(lambda: not camera.coordinator._live_owners)


async def test_authentication_failure_starts_reauth(hass, tend_camera, socket_enabled):
    camera = tend_camera.camera
    FakeTendSession.start_error = AuthenticationError("MyQ sign-in expired")
    with (
        patch.object(
            camera.coordinator.video,
            "wake_best_effort",
            AsyncMock(return_value=tend_camera.details),
        ),
        patch.object(tend_camera.entry, "async_start_reauth") as reauth,
    ):
        async with aiohttp.ClientSession() as http:
            async with http.get(await camera.stream_source()) as response:
                assert response.status == 503
    reauth.assert_called_once()
    assert tend_camera.sessions[0].closed and not camera.coordinator._live_owners


async def test_status_push_during_video_rereads_the_camera(hass, tend_camera, socket_enabled):
    camera = tend_camera.camera
    with (
        patch.object(
            camera.coordinator.video,
            "wake_best_effort",
            AsyncMock(return_value=tend_camera.details),
        ),
        patch.object(camera.coordinator, "request_camera_refresh") as refresh,
    ):
        camera._status_changed()
        refresh.assert_not_called()
        async with aiohttp.ClientSession() as http, http.get(await camera.stream_source()):
            await until(lambda: camera._live_open)
            tend_camera.sessions[0].on_status()
            refresh.assert_called_once_with(camera.device_key)


async def test_unload_closes_the_session_and_the_address(hass, tend_camera, socket_enabled):
    camera = tend_camera.camera
    url = await camera.stream_source()
    assert camera.coordinator.video_sources == {camera.device_key: camera._source}
    with patch.object(
        camera.coordinator.video, "wake_best_effort", AsyncMock(return_value=tend_camera.details)
    ):
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as response:
                await until(lambda: camera._live_open)
                assert await hass.config_entries.async_unload(tend_camera.entry.entry_id)
                await hass.async_block_till_done()
                async with asyncio.timeout(2):
                    await response.content.read()
            assert tend_camera.sessions[0].closed
            assert not camera.coordinator.video_sources and not camera.coordinator._live_owners
            with pytest.raises(aiohttp.ClientConnectorError):
                async with http.get(url):
                    pass


async def test_turning_the_camera_off_ends_video_first(hass, tend_camera, socket_enabled):
    camera = tend_camera.camera
    order = []

    async def setting(*args):
        order.append(("setting", tend_camera.sessions[0].closed))

    with (
        patch.object(
            camera.coordinator.video,
            "wake_best_effort",
            AsyncMock(return_value=tend_camera.details),
        ),
        patch.object(camera.coordinator.video, "setting", setting),
        patch.object(camera.coordinator, "execute", lambda operation: operation),
    ):
        url = await camera.stream_source()
        async with aiohttp.ClientSession() as http:
            async with http.get(url):
                await until(lambda: camera._live_open)
                await camera.async_turn_off()
            # A player reconnects at once. The camera reads as "on" until the next
            # poll, so only the pause keeps it from being reopened meanwhile.
            async with http.get(url) as response:
                assert response.status == 503
    assert order == [("setting", True)] and len(tend_camera.sessions) == 1


async def test_diagnostics_name_the_route_without_identifiers(hass, tend_camera):
    data = await async_get_config_entry_diagnostics(hass, tend_camera.entry)
    row = data["devices"][0]
    assert row["live_video_route"] == "legacy" and row["live_view_open"] is False
    assert row["live_video_source"]["sessions_started"] == 0
    assert row["camera_power"]["online"] is True and row["last_wake"] is None
    assert set(row["camera_power"]) == {
        "online",
        "sleep_mode",
        "sleep_mode_reported",
        "sleep_interval",
        "battery_reported",
        "hardware_model",
        "wakeable",
        "may_be_asleep",
    }
    assert "camera_status_connected" in data and "camera_status_error" in data
    for private in ("TC-TEST-CAMERA", "owner:camera", "OPENER-1", "test-account"):
        assert private not in str(data)


async def test_failed_poll_does_not_end_video_that_is_playing(hass, tend_camera, socket_enabled):
    """The video arrives over UDP; a cloud read that times out says nothing about it."""
    camera = tend_camera.camera
    with patch.object(
        camera.coordinator.video, "wake_best_effort", AsyncMock(return_value=tend_camera.details)
    ):
        async with (
            aiohttp.ClientSession() as http,
            http.get(await camera.stream_source()) as response,
        ):
            await until(lambda: camera._live_open)
            with patch(
                "custom_components.myq.api.client.Client.discover",
                AsyncMock(side_effect=CannotConnect("cloud timeout")),
            ):
                await camera.coordinator.async_refresh()
                await hass.async_block_till_done()
            assert not camera.coordinator.last_update_success
            assert camera._live_open and not tend_camera.sessions[0].closed
            tend_camera.sessions[0].on_video(KEYFRAME)
            async with asyncio.timeout(2):
                assert await response.content.readexactly(len(KEYFRAME)) == KEYFRAME
    assert len(tend_camera.sessions) == 1


async def test_failed_camera_details_read_keeps_the_stream_and_the_video(
    hass, tend_camera, socket_enabled
):
    camera = tend_camera.camera
    coordinator = camera.coordinator
    url = await camera.stream_source()
    with patch.object(
        coordinator.video, "wake_best_effort", AsyncMock(return_value=tend_camera.details)
    ):
        async with aiohttp.ClientSession() as http, http.get(url):
            await until(lambda: camera._live_open)
            with patch(
                "custom_components.myq.api.video.Video.details",
                AsyncMock(side_effect=CannotConnect("camera details timeout")),
            ):
                coordinator._camera_read_at.clear()  # As after any command.
                coordinator._last_full = float("-inf")
                await coordinator.async_refresh()
                await hass.async_block_till_done()
                # The poll worked; only this camera's details could not be re-read.
                assert coordinator.last_update_success
                assert coordinator.data.camera_errors == {tend_camera.device.key: "CannotConnect"}
                assert camera.supported_features & CameraEntityFeature.STREAM
                assert await camera.stream_source() == url
                assert camera._live_open and not tend_camera.sessions[0].closed
                # Details that have not been confirmed for too long are dropped.
                coordinator._camera_ok_at[tend_camera.device.key] = float("-inf")
                coordinator._last_full = float("-inf")
                await coordinator.async_refresh()
                await hass.async_block_till_done()
                assert tend_camera.device.key not in coordinator.data.cameras
                assert not camera.supported_features & CameraEntityFeature.STREAM


async def test_stopping_home_assistant_ends_the_camera_session(hass, tend_camera, socket_enabled):
    """Stopping does not remove entities, so nothing else would tell the camera to stop."""
    camera = tend_camera.camera
    with patch.object(
        camera.coordinator.video, "wake_best_effort", AsyncMock(return_value=tend_camera.details)
    ):
        async with aiohttp.ClientSession() as http:
            async with http.get(await camera.stream_source()) as response:
                await until(lambda: camera._live_open)
                hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
                await hass.async_block_till_done()
                async with asyncio.timeout(2):
                    await response.content.read()
    assert tend_camera.sessions[0].closed and not camera.coordinator._live_owners


async def test_account_flags_hand_waking_and_failing_modes_to_tend(hass, tend_camera):
    camera = tend_camera.camera
    details = {
        **tend_camera.details,
        "status": {"online": True, "pwr": {"sleepInterval": 30, "sleepMode": "LOW_POWER"}},
        "override_modes": [9],
    }
    flags = frozenset({"enable_vmw_wakeup", "enable_connection_mode_override"})
    wake = AsyncMock(return_value=details)
    with (
        patch.object(camera.coordinator.video, "account_features", AsyncMock(return_value=flags)),
        patch.object(camera.coordinator.video, "wake_best_effort", wake),
    ):
        camera.coordinator.data.cameras[camera.device_key] = details
        session = await camera._open_video(lambda record: None)
    try:
        # enable_vmw_wakeup: no HTTP wake-up; the video request asks Tend to wake it.
        assert wake.await_args.kwargs == {"post": False}
        assert (session.wakeup, session.camera_asleep) == (1, True)
        assert session.skip_modes == {9}
        data = await async_get_config_entry_diagnostics(hass, tend_camera.entry)
        row = data["devices"][0]
        assert row["override_modes_path"] == "override_modes"
        assert row["speaker"] == {
            "talkback": None,
            "full_duplex": None,
            "codecs": None,
            "talkback_codec": None,
            "playback": None,
            "stored_sounds": 0,
            "entity": False,
        }
        assert row["live_video_source"]["transport"] == session.transport
    finally:
        await camera._abandon(session)

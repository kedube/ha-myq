"""Cross-service quotas and non-replayed camera mutations with synthetic responses."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.backoff import ServiceBackoff
from custom_components.myq.api.camera_settings import CameraSettings
from custom_components.myq.api.client import Client
from custom_components.myq.api.const import ACCOUNTS, ACTIONS, DEVICES
from custom_components.myq.api.errors import (
    AuthenticationError,
    CannotConnect,
    CommandRejected,
    CommandUncertain,
    ProtocolError,
    RateLimited,
)
from custom_components.myq.api.http import Http, Response
from custom_components.myq.api.video import VIDEO, Video
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import MyQCoordinator

from .conftest import json_response
from .test_http import chunks

OWNER = {"test-account": AccountAccess("test-account", "owner", {"devices": frozenset({"All"})})}


def test_origin_cooldown_preserves_deadline_without_blocking_other_services(monkeypatch):
    clock = 10.0
    monkeypatch.setattr("custom_components.myq.api.backoff.time.monotonic", lambda: clock)
    backoff = ServiceBackoff()
    backoff.record(VIDEO, RateLimited(90, server_retry_after=True))
    clock = 25.0
    backoff.record(VIDEO, RateLimited(10))  # Shorter errors cannot lower the deadline.
    with pytest.raises(RateLimited) as error:
        backoff.check(VIDEO.upper() + ":443/cxs/api/events")
    assert error.value.retry_after == 75
    assert error.value.server_retry_after
    for url in (ACCOUNTS, ACTIONS, DEVICES, VIDEO + ":444"):
        backoff.check(url)
    clock = 100.0
    backoff.check(VIDEO)


async def test_transport_camera_quota_allows_device_request():
    session = MagicMock()
    response = session.request.return_value.__aenter__.return_value
    response.status, response.headers = 429, {"Retry-After": "90"}
    response.content.iter_chunked = MagicMock(return_value=chunks(b""))
    http = Http(session)
    with pytest.raises(RateLimited):
        await http.request("GET", VIDEO + "/cxs/api/devices/camera")
    response.status, response.headers = 200, {}
    response.content.iter_chunked = MagicMock(return_value=chunks(b"{}"))
    assert (await http.request("GET", DEVICES + "/devices")).status == 200
    with pytest.raises(RateLimited) as caught:
        await http.request("GET", VIDEO + "/cxs/api/events")
    assert 80 < caught.value.retry_after <= 90
    assert session.request.call_count == 2


async def test_client_preserves_quota_with_an_injected_transport(tokens):
    transport = AsyncMock()
    transport.request.side_effect = [
        Response(429, {"Retry-After": "90"}, b""),
        Response(204, {}, b""),
    ]
    client = Client(transport=transport, tokens=tokens)
    with pytest.raises(RateLimited):
        await client.request("GET", VIDEO, "/cxs/api/events")
    assert (await client.request("GET", DEVICES, "/devices")).status == 204
    with pytest.raises(RateLimited):
        await client.request("GET", VIDEO, "/cxs/api/devices/other")
    assert transport.request.await_count == 2


def test_record_counts_a_single_429_once_across_layers(monkeypatch):
    """The same 429 flows through the transport and Client._send; it counts once."""
    clock = 0.0
    monkeypatch.setattr("custom_components.myq.api.backoff.time.monotonic", lambda: clock)
    backoff = ServiceBackoff()
    error = RateLimited(5)  # No server Retry-After, so the streak sets the delay.
    backoff.record(DEVICES, error)
    backoff.record(DEVICES, error)  # Second layer seeing the same error is a no-op.
    with pytest.raises(RateLimited) as first:
        backoff.check(DEVICES)
    assert first.value.retry_after == 60  # One streak step (BASE_DELAY), not two.
    clock = 100.0
    again = RateLimited(5)
    backoff.record(DEVICES, again)
    backoff.record(DEVICES, again)
    with pytest.raises(RateLimited) as second:
        backoff.check(DEVICES)
    assert second.value.retry_after == 120  # Second distinct 429 doubles once, to 120.


async def test_raising_transport_streak_doubles_once_and_resets_on_success(tokens, monkeypatch):
    """A raising transport (like Http/CurlHttp) must not double-count or stick."""
    clock = 0.0
    monkeypatch.setattr("custom_components.myq.api.backoff.time.monotonic", lambda: clock)
    transport = AsyncMock()

    def answer(method, url, **kwargs):
        if transport.request.await_count <= 1 or transport.request.await_count == 3:
            # Transport records the 429 and raises, as the built-in transports do;
            # a 1-second server value leaves the streak to govern the real delay.
            error = RateLimited(1)
            client.backoff.record(url, error)
            raise error
        return Response(204, {}, b"")

    transport.request.side_effect = answer
    client = Client(transport=transport, tokens=tokens)
    client.backoff = ServiceBackoff()

    # The 429 is recorded by the transport and again by Client._send: one streak step.
    with pytest.raises(RateLimited):
        await client.request("GET", DEVICES, "/devices")
    with pytest.raises(RateLimited) as first:
        client.backoff.check(DEVICES)
    assert first.value.retry_after == 60  # BASE_DELAY once, not 120 from a double count.

    clock = 100.0  # Cooldown elapsed; a success must now clear the streak.
    assert (await client.request("GET", DEVICES, "/devices")).status == 204

    clock = 200.0
    with pytest.raises(RateLimited):
        await client.request("GET", DEVICES, "/devices")
    with pytest.raises(RateLimited) as reset:
        client.backoff.check(DEVICES)
    assert reset.value.retry_after == 60  # Streak reset by the success, so 60 again.


@pytest.mark.parametrize("status", [None, {}, {"online": False}, {"online": "true"}])
async def test_old_camera_setter_rejects_unknown_or_offline_tend_status(
    tokens, camera_device, camera_details, status
):
    camera_details["status"] = status
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(return_value=json_response(camera_details))
    with pytest.raises((CommandRejected, ProtocolError)):
        await Video(client).setting(camera_device, "mic", "enabled", False)
    assert [call.args[0] for call in client.http.request.await_args_list] == ["GET"]


async def test_old_camera_setter_uses_shared_command_lock(tokens, camera_device, camera_details):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(return_value=json_response(camera_details))
    client._command_locks[camera_device.key] = asyncio.Lock()
    async with client._command_locks[camera_device.key]:
        with pytest.raises(CommandRejected, match="already in progress"):
            await Video(client).setting(camera_device, "mic", "enabled", False)
    assert [call.args[0] for call in client.http.request.await_args_list] == ["GET"]


async def test_old_camera_setter_lost_response_is_uncertain_once(
    tokens, camera_device, camera_details
):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(
        side_effect=[json_response(camera_details), CannotConnect("lost")]
    )
    with pytest.raises(CommandUncertain):
        await Video(client).setting(camera_device, "mic", "enabled", False)
    assert [call.args[0] for call in client.http.request.await_args_list] == ["GET", "PUT"]


async def test_old_and_new_settings_reject_overlap_then_release_after_cancellation(
    tokens, camera_device
):
    client = Client(MagicMock(), tokens)
    video = Video(client)
    entered = asyncio.Event()

    async def details(_device):
        entered.set()
        await asyncio.Event().wait()

    video.details = AsyncMock(side_effect=details)
    pending = asyncio.create_task(video.setting(camera_device, "mic", "enabled", False))
    await entered.wait()
    try:
        with pytest.raises(CommandRejected, match="already in progress"):
            await CameraSettings(video).set(camera_device, "status_led", "off")
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    async with video.settings_operation(camera_device):
        pass
    assert video.details.await_count == 1


async def test_optional_quotas_preserve_new_door_state_and_stop_further_optional_reads(
    hass, tokens, door, camera_device
):
    door2 = replace(door, serial="SECOND-DOOR")
    camera2 = replace(camera_device, serial="SECOND-CAMERA")
    devices = {d.key: d for d in (door, door2, camera_device, camera2)}
    entry = MockConfigEntry(domain=DOMAIN)
    coordinator = MyQCoordinator(hass, entry, Client(MagicMock(), tokens))
    with (
        patch.object(coordinator.client, "discover", AsyncMock(return_value=devices)),
        patch.object(coordinator.management, "account_access", AsyncMock(return_value=OWNER)),
        patch.object(coordinator.garage, "read", AsyncMock(side_effect=RateLimited(120))) as garage,
        patch.object(
            coordinator.video, "details", AsyncMock(side_effect=RateLimited(240))
        ) as video,
    ):
        snapshot = await coordinator._async_update_data()
    assert snapshot.devices[door.key].door_state == "closed"
    assert snapshot.garage_errors == {d.key: "RateLimited" for d in (door, door2)}
    assert snapshot.camera_errors == {d.key: "RateLimited" for d in (camera_device, camera2)}
    garage.assert_awaited_once()
    video.assert_awaited_once()


async def test_optional_auth_failure_still_requires_reauth(hass, tokens, door):
    coordinator = MyQCoordinator(hass, MockConfigEntry(domain=DOMAIN), Client(MagicMock(), tokens))
    with (
        patch.object(coordinator.client, "discover", AsyncMock(return_value={door.key: door})),
        patch.object(coordinator.management, "account_access", AsyncMock(return_value=OWNER)),
        patch.object(
            coordinator.garage, "read", AsyncMock(side_effect=AuthenticationError("revoked"))
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator._async_update_data()


async def test_core_quota_propagates_retry_after(hass, tokens):
    coordinator = MyQCoordinator(hass, MockConfigEntry(domain=DOMAIN), Client(MagicMock(), tokens))
    with (
        patch.object(coordinator.client, "discover", AsyncMock(side_effect=RateLimited(180))),
        pytest.raises(UpdateFailed) as error,
    ):
        await coordinator._async_update_data()
    assert error.value.retry_after == 180

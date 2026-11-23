"""Home Assistant live-view lifecycle against an owned synthetic media session."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.camera import (
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCError,
    get_camera_from_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from webrtc_models import RTCIceCandidateInit

from custom_components.myq.api.errors import (
    AuthenticationError,
    CannotConnect,
    ProtocolError,
)
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import Snapshot


class FakeLiveSession:
    """Only the media boundary is replaced; actual HA setup and entity run."""

    def __init__(
        self, http, on_error, *, browser_ice_servers, allow_talkback=False, on_status=None
    ):
        self.on_error = on_error
        self.http = http
        self.browser_ice_servers = browser_ice_servers
        self.allow_talkback = allow_talkback
        self.on_status = on_status
        self.configure_controls = Mock()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.closed = False
        self.start = AsyncMock(side_effect=self._start)
        self.close = AsyncMock(side_effect=self._close)
        self.candidate = AsyncMock()
        self.join = AsyncMock(side_effect=self._join)
        self.leave = AsyncMock(side_effect=self._leave)
        self.snapshot = AsyncMock(return_value=None)
        self.ready = False
        self.joined = set()

    @property
    def viewers(self):
        return 1 + len(self.joined)

    async def _start(self, alias, token, offer, viewer="primary"):
        self.entered.set()
        await self.release.wait()
        self.ready = True
        return "synthetic-answer"

    async def _join(self, offer, viewer):
        self.joined.add(viewer)
        return "joined-answer"

    async def _leave(self, viewer, error=None):
        self.joined.discard(viewer)

    async def _close(self):
        self.close_entered.set()
        await self.close_release.wait()
        self.closed = True

    async def fail(self):
        await self.close()
        await self.on_error(CannotConnect("Synthetic stream ended"))


@pytest.fixture
async def live_camera(hass, enable_custom_integrations, tokens, camera_device, camera_details):
    camera_details["alias"] = "tend-alias-distinct-from-CAM123"
    camera_details["cap"]["cam"]["kvsWebRTC"] = True
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-live-camera-user",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    instances = []
    block_start = False

    def factory(*args, **kwargs):
        instance = FakeLiveSession(*args, **kwargs)
        if block_start:
            instance.release.clear()
        instances.append(instance)
        return instance

    def block():
        nonlocal block_start
        block_start = True

    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={camera_device.key: camera_device}),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(return_value=camera_details),
        ),
        patch("custom_components.myq.camera.LiveSession", side_effect=factory),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = hass.states.async_all("camera")[0].entity_id
        entity = get_camera_from_entity_id(hass, entity_id)
        yield SimpleNamespace(camera=entity, entry=entry, instances=instances, block_start=block)
        for instance in instances:
            instance.release.set()
            instance.close_release.set()
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("capability", "alias"),
    [(False, "alias"), (None, "alias"), (1, "alias"), (True, None), (True, ""), (True, "x" * 257)],
)
async def test_unsupported_camera_does_not_start_stream(live_camera, capability, alias):
    camera = live_camera.camera
    camera.details["cap"]["cam"]["kvsWebRTC"] = capability
    camera.details["alias"] = alias
    assert not camera.supported_features & CameraEntityFeature.STREAM
    messages = []
    await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    assert len(messages) == 1 and isinstance(messages[0], WebRTCError)
    assert messages[0].code == "unavailable"
    assert not live_camera.instances


async def test_native_offer_uses_current_token_and_tend_alias(live_camera):
    camera = live_camera.camera
    assert camera.supported_features & CameraEntityFeature.STREAM
    messages = []
    with patch.object(
        camera.coordinator.client.auth, "access_token", AsyncMock(return_value="fresh")
    ):
        await camera.async_handle_async_webrtc_offer("browser-offer", "viewer", messages.append)
    stream = live_camera.instances[0]
    assert camera.coordinator.live_sessions[camera.device_key] is stream
    assert stream in camera.coordinator._live_owners
    camera.coordinator.status_monitor.pause.assert_awaited_once()
    stream.start.assert_awaited_once_with(
        "tend-alias-distinct-from-CAM123", "fresh", "browser-offer", viewer="viewer"
    )
    assert messages == [WebRTCAnswer("synthetic-answer")]
    candidate = RTCIceCandidateInit("candidate:synthetic", sdp_mid="video", sdp_m_line_index=0)
    await camera.async_on_webrtc_candidate("viewer", candidate)
    stream.candidate.assert_awaited_once_with(candidate.to_dict(), "viewer")
    await camera.async_on_webrtc_candidate("unknown-viewer", candidate)
    assert stream.candidate.await_count == 1


async def test_idle_status_connection_resumes_after_owned_live_cleanup(hass, live_camera):
    camera = live_camera.camera
    await camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    stream = live_camera.instances[0]
    stream.close_release.clear()
    camera.close_webrtc_session("viewer")
    await stream.close_entered.wait()
    camera.coordinator.status_monitor.resume.assert_not_awaited()
    assert stream in camera.coordinator._live_owners
    assert not camera.coordinator.live_sessions
    stream.close_release.set()
    await hass.async_block_till_done()
    assert not camera.coordinator._live_owners
    camera.coordinator.status_monitor.resume.assert_awaited_once()


def _privacy_details(details):
    return {**details, "settings": {**details["settings"], "cam": {"enabled": False}}}


async def test_status_push_rereads_only_that_camera_and_is_coalesced(
    hass, live_camera, camera_details
):
    """A push re-reads one camera; it never re-polls the account or delays its poll."""
    coordinator = live_camera.camera.coordinator
    key = live_camera.camera.device_key
    entered, release = asyncio.Event(), asyncio.Event()

    async def details(device):
        entered.set()
        await release.wait()
        return _privacy_details(camera_details)

    with (
        patch.object(coordinator.video, "details", AsyncMock(side_effect=details)) as read,
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as full,
        patch.object(coordinator, "async_set_updated_data") as reschedule,
    ):
        coordinator._status_changed("unknown-camera")
        await asyncio.sleep(0)
        read.assert_not_awaited()
        for _ in range(50):
            coordinator._status_changed("tend-alias-distinct-from-CAM123")
        await entered.wait()
        release.set()
        await hass.async_block_till_done()
        read.assert_awaited_once()  # Later pushes wait for the debounce cooldown.
        full.assert_not_awaited()
        reschedule.assert_not_called()
    assert coordinator.data.cameras[key]["settings"]["cam"]["enabled"] is False
    assert live_camera.camera.is_on is False


async def test_status_push_is_suppressed_while_a_view_owns_the_session(hass, live_camera):
    coordinator = live_camera.camera.coordinator
    owner = object()
    await coordinator.async_acquire_live(owner)
    with patch.object(coordinator.video, "details", AsyncMock()) as read:
        coordinator._status_changed("tend-alias-distinct-from-CAM123")
        await hass.async_block_till_done()
    read.assert_not_awaited()
    await coordinator.async_release_live(owner)


async def test_live_view_status_push_rereads_its_camera(hass, live_camera, camera_details):
    camera = live_camera.camera
    messages = []
    await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    stream = live_camera.instances[0]
    with patch.object(
        camera.coordinator.video,
        "details",
        AsyncMock(return_value=_privacy_details(camera_details)),
    ) as read:
        stream.on_status()
        await hass.async_block_till_done()
    read.assert_awaited_once()
    assert camera.coordinator.data.cameras[camera.device_key]["settings"]["cam"]["enabled"] is False


async def test_status_refresh_failure_keeps_previous_details(hass, live_camera):
    coordinator = live_camera.camera.coordinator
    before = coordinator.data
    with patch.object(
        coordinator.video, "details", AsyncMock(side_effect=CannotConnect("lost"))
    ) as read:
        coordinator.request_camera_refresh(live_camera.camera.device_key)
        await hass.async_block_till_done()
    read.assert_awaited_once()
    assert coordinator.data is before


async def test_status_refresh_auth_failure_starts_reauth(hass, live_camera):
    coordinator = live_camera.camera.coordinator
    with patch.object(
        coordinator.video, "details", AsyncMock(side_effect=AuthenticationError("expired"))
    ):
        coordinator.request_camera_refresh(live_camera.camera.device_key)
        await hass.async_block_till_done()
    assert any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )


async def test_status_monitor_waits_for_all_live_owners(live_camera):
    coordinator = live_camera.camera.coordinator
    first, second = object(), object()
    await coordinator.async_acquire_live(first)
    await coordinator.async_acquire_live(second)
    await coordinator.async_release_live(first)
    coordinator.status_monitor.resume.assert_not_awaited()
    await coordinator.async_release_live(second)
    coordinator.status_monitor.resume.assert_awaited_once()


async def test_invalid_camera_alias_does_not_break_core_poll_snapshot(live_camera):
    coordinator = live_camera.camera.coordinator
    live_camera.camera.details["alias"] = "malformed|alias"
    coordinator._update_status_aliases(coordinator.data)
    assert not coordinator._known_aliases


async def test_one_viewer_limit_applies_during_startup_and_cleanup(hass, live_camera):
    live_camera.block_start()
    camera = live_camera.camera
    messages = []
    pending = asyncio.create_task(
        camera.async_handle_async_webrtc_offer("offer", "first", messages.append)
    )
    await asyncio.sleep(0)
    stream = live_camera.instances[0]
    await stream.entered.wait()
    await camera.async_handle_async_webrtc_offer("offer", "second", messages.append)
    assert messages[-1].code == "busy"
    assert len(live_camera.instances) == 1
    stream.release.set()
    await pending
    stream.close_release.clear()
    camera.close_webrtc_session("first")
    await stream.close_entered.wait()
    await camera.async_handle_async_webrtc_offer("offer", "second", messages.append)
    assert messages[-1].code == "busy"
    stream.close_release.set()
    await hass.async_block_till_done()
    assert stream.closed


async def test_late_failure_releases_viewer_and_allows_reopen(live_camera):
    camera = live_camera.camera
    messages = []
    await camera.async_handle_async_webrtc_offer("offer", "first", messages.append)
    first = live_camera.instances[0]
    await first.fail()
    assert first.closed and messages[-1].code == "stream_failed"
    await camera.async_handle_async_webrtc_offer("offer", "second", messages.append)
    assert len(live_camera.instances) == 2
    assert messages[-1] == WebRTCAnswer("synthetic-answer")


async def test_failed_negotiation_releases_resources_before_retry(live_camera):
    camera = live_camera.camera
    messages = []
    with patch.object(
        FakeLiveSession, "_start", AsyncMock(side_effect=CannotConnect("Negotiation failed"))
    ):
        await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    assert live_camera.instances[0].closed
    assert messages[-1].code == "stream_failed"
    await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    assert len(live_camera.instances) == 2
    assert messages[-1] == WebRTCAnswer("synthetic-answer")


async def test_failed_callback_does_not_allow_replacement_before_startup_unwinds(live_camera):
    live_camera.block_start()
    camera = live_camera.camera
    messages = []
    pending = asyncio.create_task(
        camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    )
    await asyncio.sleep(0)
    first = live_camera.instances[0]
    await first.entered.wait()
    await first.fail()
    # The failed callback released media, but the old offer handler still owns
    # startup bookkeeping. Do not let its finally block touch a replacement.
    replacement_messages = []
    retry = asyncio.create_task(
        camera.async_handle_async_webrtc_offer("offer", "viewer", replacement_messages.append)
    )
    await asyncio.sleep(0)
    try:
        assert retry.done()
        await retry
        assert replacement_messages[0].code == "busy"
    finally:
        first.release.set()
        for stream in live_camera.instances:
            stream.release.set()
        await asyncio.gather(pending, retry)


async def test_turn_off_closes_view_before_sending_setting(live_camera):
    camera = live_camera.camera
    await camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    stream = live_camera.instances[0]

    async def setting(device, section, key, value):
        assert stream.closed
        assert (section, key, value) == ("cam", "enabled", False)

    with patch.object(
        camera.coordinator.video, "setting", AsyncMock(side_effect=setting)
    ) as update:
        await camera.async_turn_off()
    update.assert_awaited_once()
    assert not camera._live_sessions


@pytest.mark.parametrize("transition", ["offline", "disabled", "removed", "capability_revoked"])
async def test_coordinator_stops_live_view(
    hass, live_camera, camera_device, camera_details, transition
):
    camera = live_camera.camera
    await camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    devices = {camera_device.key: camera_device}
    if transition == "offline":
        devices[camera_device.key] = replace(camera_device, state={"online": False})
    elif transition == "disabled":
        camera_details["settings"]["cam"]["enabled"] = False
    elif transition == "removed":
        devices.clear()
    else:
        camera_details["cap"]["cam"]["kvsWebRTC"] = False
    camera.coordinator.async_set_updated_data(
        Snapshot(devices, {camera_device.key: camera_details})
    )
    await hass.async_block_till_done()
    assert live_camera.instances[0].closed
    assert not camera._live_sessions


async def test_losing_role_data_disables_controls_but_keeps_the_view(
    hass, live_camera, camera_device, camera_details
):
    # The app has no role gate on viewing; a failed AccountAccess read must not end it.
    camera = live_camera.camera
    await camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    stream = live_camera.instances[0]
    camera.coordinator.async_set_updated_data(
        Snapshot({camera_device.key: camera_device}, {camera_device.key: camera_details})
    )
    await hass.async_block_till_done()
    assert not stream.closed and camera._live_sessions
    assert stream.configure_controls.call_args.kwargs == {"can_control": False}


async def test_unload_waits_for_stream_cleanup(hass, live_camera):
    await live_camera.camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    stream = live_camera.instances[0]
    stream.close_release.clear()
    pending = asyncio.create_task(hass.config_entries.async_unload(live_camera.entry.entry_id))
    await stream.close_entered.wait()
    assert not pending.done()
    stream.close_release.set()
    assert await pending
    assert stream.closed


async def test_unload_cancels_startup_after_failure_callback_removed_session(hass, live_camera):
    live_camera.block_start()
    pending = asyncio.create_task(
        live_camera.camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    )
    await asyncio.sleep(0)
    stream = live_camera.instances[0]
    await stream.entered.wait()
    await stream.fail()
    assert not live_camera.camera._live_sessions
    assert not pending.done()
    unloading = asyncio.create_task(hass.config_entries.async_unload(live_camera.entry.entry_id))
    try:
        async with asyncio.timeout(2):
            assert await asyncio.shield(unloading)
    finally:
        stream.release.set()
        await unloading
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stream.closed


async def test_closing_startup_cancels_negotiation_and_allows_reopen(hass, live_camera):
    live_camera.block_start()
    camera = live_camera.camera
    messages = []
    pending = asyncio.create_task(
        camera.async_handle_async_webrtc_offer("offer", "first", messages.append)
    )
    await asyncio.sleep(0)
    first = live_camera.instances[0]
    await first.entered.wait()
    camera.close_webrtc_session("first")
    with pytest.raises(asyncio.CancelledError):
        await pending
    await hass.async_block_till_done()
    assert first.closed and not messages
    replacement = asyncio.create_task(
        camera.async_handle_async_webrtc_offer("offer", "first", messages.append)
    )
    await asyncio.sleep(0)
    second = live_camera.instances[1]
    second.release.set()
    await replacement
    assert messages == [WebRTCAnswer("synthetic-answer")]


async def test_candidate_error_becomes_ha_error(live_camera):
    camera = live_camera.camera
    await camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    live_camera.instances[0].candidate.side_effect = ProtocolError("Invalid ICE candidate")
    with pytest.raises(HomeAssistantError, match="Invalid ICE candidate"):
        await camera.async_on_webrtc_candidate("viewer", RTCIceCandidateInit("invalid"))


async def test_auth_failure_closes_session_and_starts_reauth(live_camera):
    camera = live_camera.camera
    messages = []
    with (
        patch.object(
            camera.coordinator.client.auth,
            "access_token",
            AsyncMock(side_effect=AuthenticationError("Token rejected")),
        ),
        patch.object(live_camera.entry, "async_start_reauth") as reauth,
    ):
        await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    reauth.assert_called_once_with(camera.hass)
    stream = live_camera.instances[0]
    stream.start.assert_not_awaited()
    assert stream.closed and not camera._live_sessions
    assert messages[-1].code == "authentication"


async def test_device_dropped_during_offer_permission_read_is_handled(hass, live_camera):
    """The offer re-reads grants after awaits; a poll can drop the device meanwhile."""
    from custom_components.myq.api.account_access import parse_account_access

    from .test_management import access_payload

    camera = live_camera.camera
    coordinator = camera.coordinator
    grants = parse_account_access(access_payload())
    coordinator.data.account_access = grants

    async def read_while_device_disappears():
        coordinator.async_set_updated_data(Snapshot({}))
        return grants

    messages = []
    with patch.object(
        coordinator.management,
        "account_access",
        AsyncMock(side_effect=read_while_device_disappears),
    ):
        await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
        await hass.async_block_till_done()
    assert not camera._live_sessions and not camera._live_starts
    assert not coordinator._live_owners
    assert live_camera.instances[0].closed
    assert not any(isinstance(message, WebRTCAnswer) for message in messages)


async def test_second_viewer_joins_the_open_native_session(hass, live_camera):
    camera = live_camera.camera
    messages = []
    await camera.async_handle_async_webrtc_offer("offer", "first", messages.append)
    await camera.async_handle_async_webrtc_offer("offer-2", "second", messages.append)
    stream = live_camera.instances[0]
    # One MyQ session per camera: the second viewer is relayed, not a new session.
    assert len(live_camera.instances) == 1
    stream.join.assert_awaited_once_with("offer-2", "second")
    assert messages[-1] == WebRTCAnswer("joined-answer")
    candidate = RTCIceCandidateInit("candidate:synthetic", sdp_mid="video", sdp_m_line_index=0)
    await camera.async_on_webrtc_candidate("second", candidate)
    stream.candidate.assert_awaited_with(candidate.to_dict(), "second")
    # The first viewer leaving keeps the session for the second.
    camera.close_webrtc_session("first")
    await hass.async_block_till_done()
    stream.leave.assert_awaited_once_with("first")
    assert not stream.closed
    camera.close_webrtc_session("second")
    await hass.async_block_till_done()
    assert stream.closed


async def test_viewer_limit_and_shared_failure(hass, live_camera):
    camera = live_camera.camera
    messages = []
    await camera.async_handle_async_webrtc_offer("offer", "v0", messages.append)
    for index in range(1, 4):
        await camera.async_handle_async_webrtc_offer("offer", f"v{index}", messages.append)
    await camera.async_handle_async_webrtc_offer("offer", "v4", messages.append)
    assert messages[-1].code == "busy"
    stream = live_camera.instances[0]
    await stream.fail()
    # Every viewer of the shared session is told it ended.
    failures = [message for message in messages if getattr(message, "code", "") == "stream_failed"]
    assert len(failures) == 4
    assert not camera._live_sessions


async def test_snapshot_uses_the_live_frame_while_a_view_is_open(hass, live_camera):
    camera = live_camera.camera
    await camera.async_handle_async_webrtc_offer("offer", "first", lambda message: None)
    stream = live_camera.instances[0]
    stream.snapshot.return_value = b"\xff\xd8\xfflive-frame"
    with patch.object(camera.coordinator.video, "snapshot", AsyncMock()) as cloud:
        assert await camera.async_camera_image() == b"\xff\xd8\xfflive-frame"
    cloud.assert_not_awaited()
    assert hass.states.get(camera.entity_id).attributes["image_source"] == "live"


async def test_defect_during_a_start_does_not_leave_the_camera_busy(live_camera):
    """Only MyQ errors were cleaned up; anything else left the view and the status
    feed's pause behind, and every later viewer was told the camera is busy."""
    camera = live_camera.camera
    messages = []
    with (
        patch.object(FakeLiveSession, "_start", AsyncMock(side_effect=ValueError("a defect"))),
        pytest.raises(ValueError, match="a defect"),
    ):
        await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    assert live_camera.instances[0].closed and messages[-1].code == "stream_failed"
    assert not camera._live_sessions and not camera.coordinator._live_owners
    await camera.async_handle_async_webrtc_offer("offer", "viewer", messages.append)
    assert messages[-1] == WebRTCAnswer("synthetic-answer")


async def test_failed_poll_does_not_close_an_open_view(hass, live_camera):
    camera = live_camera.camera
    await camera.async_handle_async_webrtc_offer("offer", "viewer", Mock())
    with patch(
        "custom_components.myq.api.client.Client.discover",
        AsyncMock(side_effect=CannotConnect("cloud timeout")),
    ):
        await camera.coordinator.async_refresh()
        await hass.async_block_till_done()
    assert not camera.coordinator.last_update_success
    assert not live_camera.instances[0].closed and camera._live_sessions

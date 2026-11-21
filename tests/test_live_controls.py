"""Typed controls and explicit microphone consent using synthetic transports."""

import asyncio
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiortc import MediaStreamTrack
from av import AudioFrame

from custom_components.myq.api import live_controls
from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.errors import CannotConnect, CommandRejected
from custom_components.myq.api.live import LiveSession
from custom_components.myq.api.live_controls import LiveControls, MutedMicrophone
from custom_components.myq.api.video import Video
from custom_components.myq.camera_controls import camera_control_buttons


def details():
    return {
        "cap": {
            "cam": {"ptz": {}, "enableSwitch": True},
            "spk": {"talkback": True},
            "siren": {"activatable": True},
        },
        "settings": {"cam": {"enabled": True}},
        "status": {"online": True, "cam": {"ptz": {"calibrationResult": "success"}}},
    }


def controls():
    native = MagicMock(control=AsyncMock())
    result = LiveControls(native)
    result.configure(details(), can_control=True)
    return result, native


@pytest.mark.parametrize(
    "direction,wire",
    [
        ("left", ("P", "-1")),
        ("right", ("P", "1")),
        ("up", ("T", "1")),
        ("down", ("T", "-1")),
        ("zoom_in", ("Z", "1")),
        ("zoom_out", ("Z", "0")),
    ],
)
async def test_finite_native_step_and_stop_on_close(direction, wire):
    subject, native = controls()
    await subject.ptz(direction)
    native.control.assert_awaited_once_with("set-ptz", (wire,))
    with pytest.raises(CommandRejected, match="already in progress"):
        await subject.ptz(direction)
    await subject.close()
    native.control.assert_awaited_with("set-ptz", (("S", "0"),), cleanup=True)
    assert native.control.await_count == 2
    await subject.close()
    assert native.control.await_count == 2


async def test_step_watchdog_stops_once(monkeypatch):
    monkeypatch.setattr(live_controls, "PTZ_STOP_SECONDS", 0)
    subject, native = controls()
    await subject.ptz("left")
    async with asyncio.timeout(1):
        await subject._timers["ptz"]
    assert not subject._ptz_active
    await subject.close()
    assert native.control.await_count == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["status"].update(online=False),
        lambda d: d["status"].update(upgradeInProgress=True),
        lambda d: d["settings"]["cam"].update(enabled=False),
        lambda d: d["cap"]["cam"].clear(),
        lambda d: d["status"]["cam"]["ptz"].update(calibrationResult="failed"),
        lambda d: d["status"]["cam"]["ptz"].update(calibrationResult="calibrating"),
        lambda d: d["status"]["cam"]["ptz"].update(state="homing"),
        lambda d: d["status"]["cam"]["ptz"].update(state="privacy"),
        lambda d: d["status"]["cam"]["ptz"].update(state="error"),
        lambda d: d["status"]["cam"]["ptz"].update(state="future-unknown"),
        lambda d: d["status"]["cam"].update(ptz="malformed"),
    ],
)
async def test_ptz_capability_state_and_calibration_gates(mutation):
    subject, native = controls()
    value = details()
    mutation(value)
    subject.configure(value, can_control=True)
    with pytest.raises(CommandRejected):
        await subject.ptz("left")
    native.control.assert_not_awaited()
    await subject.close()


async def test_permission_revocation_preserves_stop_for_owned_siren():
    subject, native = controls()
    await subject.siren(True, duration=5)
    native.control.assert_awaited_once_with("trigger-siren-action", (("31", "01"),))
    subject.configure(details(), can_control=False)
    with pytest.raises(CommandRejected):
        await subject.ptz("up")
    await subject.siren(False)
    native.control.assert_awaited_with("trigger-siren-action", (("31", "02"),))
    await subject.close()


@pytest.mark.parametrize("feature", ["siren", "ptz"])
async def test_cancelled_close_finishes_owned_stop_without_retry(feature):
    subject, native = controls()
    if feature == "siren":
        await subject.siren(True, duration=60)
        command, actions = "trigger-siren-action", (("31", "02"),)
    else:
        await subject.ptz("left")
        command, actions = "set-ptz", (("S", "0"),)
    watchdog = subject._timers[feature]

    # A command already using the transport can delay the cleanup stop.
    async with subject._lock:
        closing = asyncio.create_task(subject.close())
        async with asyncio.timeout(1):
            await asyncio.gather(watchdog, return_exceptions=True)
        assert subject._closed
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

    # Cleanup completes without requiring another close() call to restart it.
    async with asyncio.timeout(1):
        await asyncio.shield(subject._cleanup_task)
    native.control.assert_awaited_with(command, actions, cleanup=True)
    assert native.control.await_count == 2
    assert not subject._siren_active and not subject._ptz_active
    await subject.close()
    assert native.control.await_count == 2


async def test_cancelled_close_does_not_cancel_inflight_siren_off():
    subject, native = controls()
    await subject.siren(True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop(*args, **kwargs):
        entered.set()
        await release.wait()

    native.control.side_effect = stop
    closing = asyncio.create_task(subject.close())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    finally:
        release.set()
        await subject.close()
    native.control.assert_awaited_with("trigger-siren-action", (("31", "02"),), cleanup=True)
    assert native.control.await_count == 2
    assert not subject._siren_active


@pytest.mark.parametrize("duration", [0, -1, True, 1.5, 61, "10"])
async def test_siren_duration_bounds_prevent_write(duration):
    subject, native = controls()
    with pytest.raises(CommandRejected):
        await subject.siren(True, duration=duration)
    native.control.assert_not_awaited()
    await subject.close()


async def test_unknown_write_result_never_replays_activation():
    subject, native = controls()
    native.control.side_effect = CannotConnect("outcome unknown")
    with pytest.raises(CannotConnect):
        await subject.siren(True)
    await subject.close()
    assert [call.args for call in native.control.await_args_list] == [
        ("trigger-siren-action", (("31", "01"),)),
        ("trigger-siren-action", (("31", "02"),)),
    ]


class Microphone(MediaStreamTrack):
    kind = "audio"

    async def recv(self):
        frame = AudioFrame(format="s16", layout="mono", samples=160)
        frame.sample_rate, frame.pts, frame.time_base = 8000, 0, Fraction(1, 8000)
        frame.planes[0].update(b"\x01\x02" * 160)
        return frame


async def test_microphone_sends_silence_until_explicit_enable_and_auto_mutes(monkeypatch):
    monkeypatch.setattr(live_controls, "MAX_TALKBACK_SECONDS", 0)
    source = Microphone()
    track = MutedMicrophone(source)
    silent = await track.recv()
    assert bytes(silent.planes[0]) == b"\0" * 320
    assert silent.sample_rate == 8000 and silent.time_base == Fraction(1, 8000)
    track.enable(True)
    assert bytes((await track.recv()).planes[0]) == b"\x01\x02" * 160
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not track.enabled
    assert bytes((await track.recv()).planes[0]) == b"\0" * 320
    track.stop()
    assert source.readyState == "ended"


async def test_camera_buttons_refresh_permission_and_keep_stop_after_revocation(camera_device):
    stream = LiveSession(None, AsyncMock())
    stream._ready = True
    stream._controls, native = controls()
    access = AccountAccess(camera_device.account_id, "owner", {"devices": frozenset({"All"})})
    video = Video(None)
    video.details = AsyncMock(return_value=details())
    coordinator = MagicMock()
    coordinator.data = SimpleNamespace(
        devices={camera_device.key: camera_device},
        cameras={camera_device.key: details()},
        account_access={camera_device.account_id: access},
    )
    coordinator.last_update_success = True
    coordinator.live_sessions = {camera_device.key: stream}
    coordinator.video = video
    coordinator.management.account_access = AsyncMock(return_value={})

    async def execute(operation):
        return await operation

    coordinator.execute = execute
    buttons = {
        button.action: button for button in camera_control_buttons(coordinator, camera_device)
    }
    assert len(buttons) == 11
    assert buttons["left"].available
    with pytest.raises(CommandRejected, match="not permitted"):
        await buttons["left"].async_press()
    native.control.assert_not_awaited()
    video.details.assert_awaited_once_with(camera_device)
    coordinator.management.account_access.assert_awaited_once()
    # Permission lookup fails after revocation: explicit stop still reaches the owned session.
    coordinator.management.account_access.side_effect = CannotConnect("unavailable")
    await buttons["stop"].async_press()
    native.control.assert_awaited_once_with("set-ptz", (("S", "0"),))
    await stream.close()


def button_coordinator(camera_device, stream, camera, access):
    coordinator = MagicMock()
    coordinator.data = SimpleNamespace(
        devices={camera_device.key: camera_device},
        cameras={camera_device.key: camera},
        account_access={camera_device.account_id: access},
    )
    coordinator.last_update_success = True
    coordinator.live_sessions = {camera_device.key: stream}
    coordinator.video = Video(None)

    async def execute(operation):
        return await operation

    coordinator.execute = execute
    return coordinator


async def test_camera_button_press_reads_grants_and_details_together(camera_device):
    """Both fresh reads must overlap; run one after the other, this would time out."""
    stream = LiveSession(None, AsyncMock())
    stream._ready = True
    stream._controls, native = controls()
    access = AccountAccess(camera_device.account_id, "owner", {"devices": frozenset({"All"})})
    coordinator = button_coordinator(camera_device, stream, details(), access)
    started, both = 0, asyncio.Event()

    async def overlapping(value):
        nonlocal started
        started += 1
        if started == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        return value

    async def read_details(device):
        return await overlapping(details())

    async def read_grants():
        return await overlapping({camera_device.account_id: access})

    coordinator.video.details = AsyncMock(side_effect=read_details)
    coordinator.management.account_access = AsyncMock(side_effect=read_grants)
    (left,) = [b for b in camera_control_buttons(coordinator, camera_device) if b.action == "left"]
    await left.async_press()
    native.control.assert_awaited_once_with("set-ptz", (("P", "-1"),))
    await stream.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["settings"]["cam"].update(enabled=False),  # Privacy mode.
        lambda d: d["status"].update(upgradeInProgress=True),
        lambda d: d["status"]["cam"]["ptz"].update(calibrationResult="failed"),
    ],
)
async def test_camera_buttons_are_unavailable_when_movement_is_unsafe(camera_device, change):
    stream = LiveSession(None, AsyncMock())
    camera = details()
    change(camera)
    access = AccountAccess(camera_device.account_id, "owner", {"devices": frozenset({"All"})})
    coordinator = button_coordinator(camera_device, stream, camera, access)
    buttons = {b.action: b for b in camera_control_buttons(coordinator, camera_device)}
    assert not buttons["left"].available
    assert buttons["stop"].available  # Stopping is never gated on readiness or grants.
    coordinator.data.account_access = {}
    assert buttons["stop"].available and not buttons["siren_on"].available

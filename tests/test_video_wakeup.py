"""Live-video availability follows the native wake command and power states."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from custom_components.myq.api.errors import (
    AccessDenied,
    CannotConnect,
    CommandRejected,
    ProtocolError,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.video import Video


def response(value):
    return Response(200, {}, json.dumps(value).encode())


def details(mode="FULL_POWER", *, interval=0, platform="MyQ:Chicony-MSC528-II"):
    return {
        "settings": {"cam": {"enabled": True}},
        "cap": {"cam": {"kvsWebRTC": True}},
        "hw": {"platform": platform},
        "status": {"online": True, "pwr": {"sleepInterval": interval, "sleepMode": mode}},
    }


async def test_wired_awake_camera_skips_wakeup(camera_device):
    client = AsyncMock()
    info = details()
    assert await Video(client).wakeup(camera_device, info) is info
    client.request.assert_not_called()


@pytest.mark.parametrize("initial_mode", ["FULL_POWER", "LOW_POWER_DEEP_SLEEP"])
async def test_battery_model_wakes_even_when_currently_awake(camera_device, initial_mode):
    client = AsyncMock()
    before = details(initial_mode, platform="MyQ:PMX-SAV524MT7686-STM-II")
    after = details()
    client.request.side_effect = [Response(204, {}, b""), response(after)]
    assert await Video(client).wakeup(camera_device, before) == after
    command = client.request.call_args_list[0]
    assert command.args == (
        "POST",
        "https://media.hubs.tend-us.tendplatform.com",
        f"/cxs/api/devices/{camera_device.camera_id}/do/wakeup",
    )
    assert command.kwargs == {"headers": {"AntiCSRF": "AntiCSRF"}, "json": {}}
    assert client.request.call_args_list[1].args[0] == "GET"


async def test_sleep_interval_requires_wakeup_and_waits_for_full_power(camera_device, monkeypatch):
    monkeypatch.setattr("custom_components.myq.api.video.WAKEUP_POLL_INTERVAL", 0)
    client = AsyncMock()
    before = details("LOW_POWER_DEEP_SLEEP", interval=10)
    booting = details("FULL_POWER_BOOT", interval=10)
    awake = details("FULL_POWER", interval=10)
    client.request.side_effect = [Response(204, {}, b""), response(booting), response(awake)]
    assert await Video(client).wakeup(camera_device, before) == awake
    assert [call.args[0] for call in client.request.call_args_list] == ["POST", "GET", "GET"]


async def test_wakeup_rejection_aborts_without_read_or_replay(camera_device):
    client = AsyncMock()
    client.request.side_effect = AccessDenied("MyQ rejected camera wakeup")
    with pytest.raises(AccessDenied):
        await Video(client).wakeup(camera_device, details(interval=10))
    assert client.request.call_count == 1


async def test_wakeup_wait_is_bounded_without_repeating_command(camera_device, monkeypatch):
    monkeypatch.setattr("custom_components.myq.api.video.WAKEUP_TIMEOUT", 0.02)
    client = AsyncMock()
    before = details("LOW_POWER_DEEP_SLEEP", interval=10)
    client.request.side_effect = [Response(204, {}, b""), response(before)]
    with pytest.raises(CannotConnect, match="did not wake up"):
        await Video(client).wakeup(camera_device, before)
    assert [call.args[0] for call in client.request.call_args_list] == ["POST", "GET"]


async def test_cancel_after_wakeup_does_not_repeat_command(camera_device):
    client = AsyncMock()
    polling = asyncio.Event()

    async def request(method, *_args, **_kwargs):
        if method == "POST":
            return Response(204, {}, b"")
        polling.set()
        await asyncio.Future()

    client.request.side_effect = request
    task = asyncio.create_task(Video(client).wakeup(camera_device, details(interval=10)))
    await polling.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call.args[0] for call in client.request.call_args_list] == ["POST", "GET"]


@pytest.mark.parametrize("status", [[], {"online": True, "pwr": []}])
async def test_malformed_availability_cannot_start_video(camera_device, status):
    client = AsyncMock()
    info = details()
    info["status"] = status
    with pytest.raises(ProtocolError):
        await Video(client).wakeup(camera_device, info)
    client.request.assert_not_called()


@pytest.mark.parametrize("status", [None, {}, {"online": "true"}, {"online": False}])
async def test_missing_or_non_boolean_online_reads_as_offline(camera_device, status):
    # i41/l reads status.online with a false default.
    client = AsyncMock()
    info = details()
    info["status"] = status
    with pytest.raises(CommandRejected, match="unavailable"):
        await Video(client).wakeup(camera_device, info)
    client.request.assert_not_called()


@pytest.mark.parametrize("power", [{}, {"pwrSrc": "AC", "battLv": 100}])
async def test_powered_camera_without_a_sleep_mode_is_available(camera_device, power):
    # VideoDeviceInfo.c(): no sleep mode means AVAILABLE unless sleepInterval > 0.
    client = AsyncMock()
    info = details()
    info["status"]["pwr"] = power
    assert await Video(client).wakeup(camera_device, info) is info
    client.request.assert_not_called()


async def test_sleep_mode_is_case_insensitive(camera_device):
    client = AsyncMock()
    info = details("full_power")
    assert await Video(client).wakeup(camera_device, info) is info
    client.request.assert_not_called()


async def test_model_display_name_does_not_trigger_wakeup(camera_device):
    client = AsyncMock()
    info = details()
    info["hw"]["model"] = "PMX-SAV524MT7686-STM-II"
    await Video(client).wakeup(camera_device, info)
    client.request.assert_not_called()


async def test_unknown_power_mode_after_wakeup_keeps_waiting(camera_device, monkeypatch):
    # An unknown mode reads as absent, which a timer-sleeping camera treats as asleep.
    monkeypatch.setattr("custom_components.myq.api.video.WAKEUP_POLL_INTERVAL", 0)
    client = AsyncMock()
    before = details(interval=10)
    unknown = copy.deepcopy(before)
    unknown["status"]["pwr"]["sleepMode"] = "UNKNOWN"
    awake = details(interval=10)
    client.request.side_effect = [Response(204, {}, b""), response(unknown), response(awake)]
    assert await Video(client).wakeup(camera_device, before) == awake
    assert client.request.call_count == 3


async def test_unknown_power_mode_is_woken_before_live_view(camera_device, monkeypatch):
    # SleepModeDeserializer maps unrecognized values to UNKNOWN, which is not available.
    monkeypatch.setattr("custom_components.myq.api.video.WAKEUP_POLL_INTERVAL", 0)
    client = AsyncMock()
    odd = details()
    odd["status"]["pwr"]["sleepMode"] = "odd"
    client.request.side_effect = [Response(204, {}, b""), response(details())]
    assert await Video(client).wakeup(camera_device, odd) == details()
    assert client.request.call_args_list[0].args[0] == "POST"


@pytest.mark.parametrize(
    "power,platform,sleeps",
    [
        ({"sleepInterval": 0}, "MyQ:PMX-SAV524MT7686-STM-II", False),  # pwr decides.
        ({"sleepInterval": 30}, "MyQ:Chicony-MSC528-II", True),
        (None, "MyQ:PMX-RTL8735BM-AL4-VII", True),  # CO video keypad model.
        (None, "MyQ:Chicony-MSC528-II", False),  # Garage camera, mains powered.
    ],
)
def test_settings_sleep_rule_follows_video_device_info(power, platform, sleeps):
    from custom_components.myq.api.video import _sleeps

    info = details()
    info["hw"]["platform"] = platform
    if power is None:
        info["status"].pop("pwr")
    else:
        info["status"]["pwr"] = power
    assert _sleeps(info) is sleeps


async def test_settings_wake_polls_every_two_seconds_then_writes_if_online(
    camera_device, monkeypatch
):
    monkeypatch.setattr("custom_components.myq.api.video.SETTINGS_WAKE_TIMEOUT", 0.05)
    monkeypatch.setattr("custom_components.myq.api.video.SETTINGS_WAKE_INTERVAL", 0.01)
    client = AsyncMock()
    asleep = details("LOW_POWER_DEEP_SLEEP", interval=10)
    client.request.side_effect = [response(asleep), Response(204, {}, b"")] + [
        response(asleep)
    ] * 50
    # The app's handling of a failed wake did not decompile; an online camera is written.
    assert await Video(client).ready(camera_device) == asleep

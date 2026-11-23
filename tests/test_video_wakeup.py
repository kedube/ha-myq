"""Live-video availability follows the native wake command and power states."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from custom_components.myq.api.errors import (
    AccessDenied,
    CannotConnect,
    CommandRejected,
    ProtocolError,
    RateLimited,
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
    assert await Video(client).wake_best_effort(camera_device, info) is info
    client.request.assert_not_called()


async def test_battery_model_reporting_full_power_is_not_nudged(camera_device):
    # The video request itself asks Tend to wake a battery model (may_be_asleep).
    client = AsyncMock()
    info = details(platform="MyQ:PMX-SAV524MT7686-STM-II")
    assert await Video(client).wake_best_effort(camera_device, info) is info
    client.request.assert_not_called()


async def test_sleeping_battery_model_gets_one_wake_command(camera_device):
    client = AsyncMock()
    before = details("LOW_POWER_DEEP_SLEEP", platform="MyQ:PMX-SAV524MT7686-STM-II")
    after = details()
    client.request.side_effect = [Response(204, {}, b""), response(after)]
    assert await Video(client).wake_best_effort(camera_device, before) == after
    command = client.request.call_args_list[0]
    assert command.args == (
        "POST",
        "https://media.hubs.tend-us.tendplatform.com",
        f"/cxs/api/devices/{camera_device.camera_id}/do/wakeup",
    )
    assert command.kwargs == {"headers": {"AntiCSRF": "AntiCSRF"}, "json": {}}
    assert client.request.call_args_list[1].args[0] == "GET"


async def test_wake_rejection_proceeds_without_read_or_replay(camera_device):
    client = AsyncMock()
    before = details("LOW_POWER_DEEP_SLEEP", interval=10)
    client.request.side_effect = AccessDenied("MyQ rejected camera wakeup")
    assert await Video(client).wake_best_effort(camera_device, before) == before
    assert client.request.call_count == 1


async def test_cancel_after_wakeup_does_not_repeat_command(camera_device):
    client = AsyncMock()
    polling = asyncio.Event()

    async def request(method, *_args, **_kwargs):
        if method == "POST":
            return Response(204, {}, b"")
        polling.set()
        await asyncio.Future()

    client.request.side_effect = request
    asleep = details("LOW_POWER_DEEP_SLEEP", interval=10)
    task = asyncio.create_task(Video(client).wake_best_effort(camera_device, asleep))
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
        await Video(client).wake_best_effort(camera_device, info)
    client.request.assert_not_called()


@pytest.mark.parametrize("status", [None, {}, {"online": "true"}, {"online": False}])
async def test_missing_or_non_boolean_online_reads_as_offline(camera_device, status):
    # i41/l reads status.online with a false default.
    client = AsyncMock()
    info = details()
    info["status"] = status
    with pytest.raises(CommandRejected, match="unavailable"):
        await Video(client).wake_best_effort(camera_device, info)
    client.request.assert_not_called()


@pytest.mark.parametrize("power", [{}, {"pwrSrc": "AC", "battLv": 100}])
async def test_powered_camera_without_a_sleep_mode_is_available(camera_device, power):
    # VideoDeviceInfo.c(): no sleep mode means AVAILABLE unless sleepInterval > 0.
    client = AsyncMock()
    info = details()
    info["status"]["pwr"] = power
    assert await Video(client).wake_best_effort(camera_device, info) is info
    client.request.assert_not_called()


async def test_sleep_mode_is_case_insensitive(camera_device):
    client = AsyncMock()
    info = details("full_power")
    assert await Video(client).wake_best_effort(camera_device, info) is info
    client.request.assert_not_called()


async def test_model_display_name_does_not_trigger_wakeup(camera_device):
    client = AsyncMock()
    info = details()
    info["hw"]["model"] = "PMX-SAV524MT7686-STM-II"
    await Video(client).wake_best_effort(camera_device, info)
    client.request.assert_not_called()


async def test_unknown_power_mode_is_woken_before_live_view(camera_device):
    # SleepModeDeserializer maps unrecognized values to UNKNOWN, which is not available.
    client = AsyncMock()
    odd = details()
    odd["status"]["pwr"]["sleepMode"] = "odd"
    client.request.side_effect = [Response(204, {}, b""), response(details())]
    assert await Video(client).wake_best_effort(camera_device, odd) == details()
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


async def test_best_effort_wake_available_camera_sends_no_post(camera_device):
    client = AsyncMock()
    info = details()  # mains-powered, FULL_POWER
    assert await Video(client).wake_best_effort(camera_device, info) is info
    client.request.assert_not_called()


async def test_best_effort_wake_does_not_block_on_full_power(camera_device, monkeypatch):
    # The exact real-camera failure: a sleeping camera that never reports FULL_POWER.
    # wakeup() would time out ("did not wake up in time"); wake_best_effort must not.
    monkeypatch.setattr("custom_components.myq.api.video.WAKEUP_POLL_INTERVAL", 0.01)
    client = AsyncMock()
    asleep = details("LOW_POWER_DEEP_SLEEP", interval=10)
    # POST ok, then a re-read that is still not FULL_POWER.
    client.request.side_effect = [Response(204, {}, b""), response(asleep)]
    result = await Video(client).wake_best_effort(camera_device, asleep)
    # It proceeds with fresh details instead of raising.
    assert result == asleep
    assert [call.args[0] for call in client.request.call_args_list] == ["POST", "GET"]


async def test_best_effort_wake_proceeds_even_if_the_post_fails(camera_device):
    client = AsyncMock()
    before = details("LOW_POWER_DEEP_SLEEP", interval=10)
    client.request.side_effect = CannotConnect("MyQ wakeup POST failed")
    # A failed nudge must not block the stream; the Tend session may still wake it.
    result = await Video(client).wake_best_effort(camera_device, before)
    assert result == before
    assert client.request.call_count == 1


async def test_best_effort_wake_still_propagates_fatal_failures(camera_device):
    # Auth and rate-limit errors from the nudge must surface, not be swallowed.
    client = AsyncMock()
    client.request.side_effect = RateLimited(30)
    asleep = details("LOW_POWER_DEEP_SLEEP", interval=10)
    with pytest.raises(RateLimited):
        await Video(client).wake_best_effort(camera_device, asleep)


async def test_best_effort_wake_refuses_an_offline_unwakeable_camera(camera_device):
    client = AsyncMock()
    info = details()  # mains, no sleep interval, non-wakeup model
    info["status"]["online"] = False
    with pytest.raises(CommandRejected, match="unavailable"):
        await Video(client).wake_best_effort(camera_device, info)
    client.request.assert_not_called()


@pytest.mark.parametrize(
    "info,expected",
    [
        (details(), False),  # Mains-powered model reporting FULL_POWER.
        (details("LOW_POWER_DEEP_SLEEP"), True),
        (details(interval=30), True),  # Sleeps on a timer.
        (details(platform="MyQ:PMX-SAV524MT7686-STM-II"), True),  # Battery model.
        ({**details(), "status": {"online": False}}, True),
        ({**details(), "status": {"online": True, "pwr": []}}, True),  # Unreadable power data.
    ],
)
def test_may_be_asleep_decides_the_video_request_wakeup(info, expected):
    from custom_components.myq.api.video import may_be_asleep

    assert may_be_asleep(info) is expected


def test_absurd_sleep_interval_is_a_protocol_error():
    from custom_components.myq.api.errors import ProtocolError
    from custom_components.myq.api.video import _sleep_interval

    with pytest.raises(ProtocolError, match="sleep interval"):
        _sleep_interval({"sleepInterval": 10**400})


@pytest.mark.parametrize(
    "info,expected",
    [
        (
            details("LOW_POWER_DEEP_SLEEP", interval=30, platform="MyQ:PMX-SAV524MT7686-STM-II"),
            {
                "online": True,
                "sleep_mode": "LOW_POWER_DEEP_SLEEP",
                "sleep_mode_reported": None,
                "sleep_interval": 30,
                "battery_reported": False,
                "hardware_model": "PMX-SAV524MT7686-STM-II",
                "wakeable": True,
                "may_be_asleep": True,
            },
        ),
        (
            {"status": {"online": "yes", "pwr": {"sleepInterval": 10**400, "battLv": 80}}},
            {
                "online": False,
                "sleep_mode": None,
                "sleep_mode_reported": None,
                "sleep_interval": "invalid",
                "battery_reported": True,
                "hardware_model": None,
                "wakeable": False,
                "may_be_asleep": True,
            },
        ),
        (
            {"status": [], "hw": {"platform": 7}},
            {
                "online": False,
                "sleep_mode": None,
                "sleep_mode_reported": None,
                "sleep_interval": None,
                "battery_reported": False,
                "hardware_model": "invalid",
                "wakeable": False,
                "may_be_asleep": True,
            },
        ),
    ],
)
def test_power_summary_reports_the_wake_inputs_and_survives_bad_data(info, expected):
    from custom_components.myq.api.video import power_summary

    assert power_summary(info) == expected


async def test_wake_outcome_is_kept_for_diagnostics(camera_device):
    client = AsyncMock()
    video = Video(client)
    await video.wake_best_effort(camera_device, details())
    assert video.wakes[camera_device.key] == "not_needed"
    client.request.side_effect = CannotConnect("down")
    await video.wake_best_effort(camera_device, details("LOW_POWER_DEEP_SLEEP", interval=10))
    assert video.wakes[camera_device.key] == "failed: CannotConnect"
    client.request.side_effect = [Response(204, {}, b""), response(details())]
    await video.wake_best_effort(camera_device, details("LOW_POWER_DEEP_SLEEP", interval=10))
    assert video.wakes[camera_device.key] == "sent"


@pytest.mark.parametrize(
    "features,asleep,expected",
    [
        # Flags unread: an HTTP wake-up and wakeup=1 for a camera that may sleep.
        (None, True, (True, 1)),
        (None, False, (True, None)),
        # enable_vmw_wakeup: Tend's video service owns the wake-up (wakeup=1 or 0).
        (frozenset({"enable_vmw_wakeup"}), True, (False, 1)),
        (frozenset({"enable_vmw_wakeup"}), False, (True, 0)),
        # Without it: the HTTP wake-up and no field, as the app sends.
        (frozenset({"other_flag"}), True, (True, None)),
        (frozenset(), False, (True, None)),
    ],
)
def test_live_wake_plan_follows_the_account_flag(features, asleep, expected):
    from custom_components.myq.api.video import live_wake_plan

    info = details("LOW_POWER_DEEP_SLEEP", interval=10) if asleep else details()
    assert live_wake_plan(info, features) == expected


@pytest.mark.parametrize(
    "info,expected",
    [
        (
            {"status": {"override_modes": [9, "10", "x", 99, True]}},
            ({9, 10}, "status.override_modes"),
        ),
        ({"cap": {"cam": {"overrideModes": []}}}, (set(), "cap.cam.overrideModes")),
        ({"override_modes": "9"}, (set(), "override_modes")),  # Not a list: unused.
        ({"a": {"b": {"c": {"d": {"e": {"override_modes": [9]}}}}}}, (set(), None)),  # Too deep.
        (details(), (set(), None)),
    ],
)
def test_override_modes_are_found_in_the_device_document(info, expected):
    from custom_components.myq.api.video import override_modes

    modes, path = override_modes(info)
    assert (set(modes), path) == expected


async def test_account_flags_are_read_once_an_hour_and_kept_through_a_failure(monkeypatch):
    from custom_components.myq.api import video as video_module

    clock = [1000.0]
    monkeypatch.setattr(video_module.time, "monotonic", lambda: clock[0])
    client = AsyncMock()
    client.features.side_effect = [
        CannotConnect("down"),
        frozenset({"enable_vmw_wakeup"}),
        CannotConnect("down again"),
    ]
    video = Video(client)
    assert video.live_flags is None
    assert await video.account_features() is None
    clock[0] += 60  # A failed read is not retried at once.
    assert await video.account_features() is None and client.features.await_count == 1
    clock[0] += 300
    assert await video.account_features() == {"enable_vmw_wakeup"}
    assert video.live_flags == {
        "enable_vmw_wakeup": True,
        "enable_connection_mode_override": False,
        "enable_new_live_stream_connection_flow": False,
    }
    clock[0] += 1800
    assert await video.account_features() == {"enable_vmw_wakeup"}
    assert client.features.await_count == 2
    clock[0] += 1800  # Due again; the read fails, and the flags read earlier stay.
    assert await video.account_features() == {"enable_vmw_wakeup"}
    assert client.features.await_count == 3


async def test_client_reads_the_feature_list(tokens, http):
    from custom_components.myq.api.client import Client
    from custom_components.myq.api.const import PROFILES

    http.request.return_value = response(
        {"features": [{"feature_id": "enable_vmw_wakeup"}, {"feature_id": 5}, "x", {}]}
    )
    assert await Client(transport=http, tokens=tokens).features() == {"enable_vmw_wakeup"}
    assert http.request.call_args.args[:2] == ("GET", PROFILES + "/api/v6.0/my/features")
    http.request.return_value = response({"features": "none"})
    with pytest.raises(ProtocolError):
        await Client(transport=http, tokens=tokens).features()


async def test_wake_left_to_tend_sends_no_post(camera_device):
    client = AsyncMock()
    video = Video(client)
    asleep = details("LOW_POWER_DEEP_SLEEP", interval=10)
    assert await video.wake_best_effort(camera_device, asleep, post=False) is asleep
    client.request.assert_not_called()
    assert video.wakes[camera_device.key] == "left_to_tend"


@pytest.mark.parametrize(
    "raw,reported",
    [
        ("ALWAYS_ON", "ALWAYS_ON"),  # Not one of the three known codes: shown as sent.
        ("full_power", None),  # Known (case-insensitive): nothing extra.
        ("not a code!", "invalid"),
        ("X" * 41, "invalid"),
        (7, "invalid"),
    ],
)
def test_unrecognised_sleep_mode_is_reported_as_sent(raw, reported):
    from custom_components.myq.api.video import power_summary

    info = details()
    info["status"]["pwr"]["sleepMode"] = raw
    summary = power_summary(info)
    assert summary["sleep_mode_reported"] == reported
    assert summary["sleep_mode"] == ("FULL_POWER" if reported is None else "UNKNOWN")

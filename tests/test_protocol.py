"""Exercise protocol boundaries, token concurrency, and command semantics."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import pytest

from custom_components.myq.api.client import Client
from custom_components.myq.api.const import ACCOUNTS, ACTIONS
from custom_components.myq.api.errors import (
    AccessDenied,
    AuthenticationError,
    CannotConnect,
    CommandRejected,
    CommandUncertain,
    ProtocolError,
    RateLimited,
    RequestUnauthorized,
)
from custom_components.myq.api.http import Http, Response, retry_seconds
from custom_components.myq.api.models import Device
from custom_components.myq.api.tokens import TokenManager, Tokens
from custom_components.myq.api.video import Video

from .conftest import json_response


async def test_refresh_is_single_flight_and_rotated_token_is_persisted(http):
    http.request.return_value = json_response(
        {
            "access_token": "new-access",
            "refresh_token": "rotated",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
    )
    save = AsyncMock()
    manager = TokenManager(http, Tokens("old", "old-refresh", 0), save)
    result = await asyncio.gather(*(manager.access_token() for _ in range(20)))
    assert result == ["new-access"] * 20
    assert http.request.await_count == 1
    assert parse_qs(http.request.call_args.kwargs["data"].decode())["refresh_token"] == [
        "old-refresh"
    ]
    save.assert_awaited_once_with(manager.tokens)
    assert manager.tokens.refresh_token == "rotated"
    assert "rotated" not in repr(manager.tokens)


async def test_401_read_refreshes_once_without_extra_refresh_for_concurrent_401(tokens):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(
        side_effect=[
            Response(401, {}, b""),
            json_response({"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}),
            json_response({"accounts": []}),
        ]
    )
    assert await client.accounts() == []
    assert client.http.request.await_count == 3
    assert await client.auth.access_token(rejected_token=tokens.access_token) == "new"
    assert client.http.request.await_count == 3


async def test_refresh_rejection_requests_reauth(http, tokens):
    http.request.return_value = json_response({"error": "invalid_grant"}, 400)
    manager = TokenManager(http, Tokens(tokens.access_token, tokens.refresh_token, 0))
    with pytest.raises(AuthenticationError):
        await manager.access_token()
    assert manager.tokens.refresh_token == tokens.refresh_token


@pytest.mark.parametrize("expiry", [0, -1, float("inf"), float("nan"), None, "wrong", True])
def test_unusable_expiry_takes_the_apps_one_hour_default(expiry):
    # ip0/j.I0: a rotation is never discarded over its lifetime field.
    tokens = Tokens.from_response({"access_token": "x", "refresh_token": "y", "expires_in": expiry})
    assert tokens.lifetime == 3600
    assert Tokens.from_response({"access_token": "x", "refresh_token": "y"}).lifetime == 3600


def test_token_type_is_ignored_and_missing_access_token_is_rejected():
    data = {"access_token": "x", "refresh_token": "y", "expires_in": 60, "token_type": "dpop"}
    assert Tokens.from_response(data).access_token == "x"
    for access in (None, "", 7):
        with pytest.raises(ProtocolError):
            Tokens.from_response({"access_token": access, "refresh_token": "y"})
    # An omitted or empty refresh token keeps the current one.
    kept = Tokens.from_response({"access_token": "x", "refresh_token": None}, "current")
    assert kept.refresh_token == "current"


@pytest.mark.parametrize("action", ["open", "close"])
async def test_exact_door_command_route_and_body(tokens, door, action):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(return_value=Response(204, {}, b""))
    await client.command(door, action)
    args = client.http.request.call_args
    assert args.args == ("PUT", ACTIONS + "/api/v1.0/accounts/test-account/devices/TEST-GDO-1")
    assert args.kwargs["json"] == {
        "action": action,
        "trigger": "myq_an",
        "device_family": "garagedoor",
    }
    assert args.kwargs["headers"] == {
        "Authorization": "Bearer " + tokens.access_token,
        "BrandId": "1",
        "App-Version": "5.324.0.1132442",
        "User-Agent": "sdk_gphone64_arm64/Android 17",
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
    }


@pytest.mark.parametrize("error", [CannotConnect("timeout"), CannotConnect("HTTP 503")])
async def test_mutation_failure_is_not_replayed(tokens, door, error):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(side_effect=error)
    with pytest.raises(CommandUncertain):
        await client.command(door, "open")
    assert client.http.request.await_count == 1


async def test_mutation_401_is_renewed_and_sent_once_more(tokens, door):
    # ft0/c1: a 401 means MyQ did not act; the app renews and resends any method once.
    client = Client(MagicMock(), tokens)
    renewed = json_response({"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    client.http.request = AsyncMock(
        side_effect=[Response(401, {}, b""), renewed, Response(401, {}, b"")]
    )
    with pytest.raises(RequestUnauthorized):
        await client.command(door, "open")
    calls = client.http.request.call_args_list
    assert [call.args[0] for call in calls] == ["PUT", "POST", "PUT"]
    assert calls[2].kwargs["headers"]["Authorization"] == "Bearer new"
    assert client.auth.tokens.refresh_token == "r2"


async def test_mutation_401_with_another_code_is_not_renewed(tokens, door):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(return_value=json_response({"code": "401.122"}, 401))
    with pytest.raises(RequestUnauthorized):
        await client.command(door, "open")
    assert client.http.request.await_count == 1


@pytest.mark.parametrize("body", [b"", b"<html>", b'{"code": "401.102"}', b'{"code": 401}'])
async def test_renewable_401_answers_follow_the_app(tokens, body):
    client = Client(MagicMock(), tokens)
    renewed = json_response({"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    client.http.request = AsyncMock(
        side_effect=[Response(401, {}, body), renewed, json_response({"user_id": "u"})]
    )
    assert (await client.profile())["user_id"] == "u"


@pytest.mark.parametrize(
    "field,value",
    [
        ("online", False),
        ("online", "true"),
        ("is_unattended_close_allowed", False),
        ("is_unattended_close_allowed", "true"),
        ("monitor_only_mode", True),
        ("monitor_only_mode", "true"),
        ("monitor_only_mode", "false"),
        ("monitor_only_mode", 1),
        ("monitor_only_mode", 0),
        ("monitor_only_mode", {}),
    ],
)
async def test_door_safety_flags_prevent_request(tokens, door_payload, field, value):
    door_payload["state"][field] = value
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock()
    with pytest.raises(CommandRejected):
        await client.command(Device.parse("account", door_payload), "close")
    client.http.request.assert_not_awaited()


async def test_no_stop_or_guessed_percentage_command(tokens, door):
    client = Client(MagicMock(), tokens)
    with pytest.raises(CommandRejected):
        await client.command(door, "stop")


@pytest.mark.parametrize("action", ["open", "close"])
@pytest.mark.parametrize("permission", [False, "true", 1])
async def test_each_door_action_requires_its_own_explicit_permission(
    tokens, door_payload, action, permission
):
    door_payload["state"][f"is_unattended_{action}_allowed"] = permission
    device = Device.parse("test-account", door_payload)
    client = Client(transport=AsyncMock(), tokens=tokens)
    with pytest.raises(CommandRejected):
        await client.command(device, action)
    client.http.request.assert_not_awaited()
    assert device.can_move("close" if action == "open" else "open")


def test_absent_flags_use_the_app_defaults(door_payload):
    # Device.m1 defaults to false and A1 to true; the app never reads the open flag.
    del door_payload["state"]["monitor_only_mode"]
    del door_payload["state"]["is_unattended_open_allowed"]
    del door_payload["state"]["is_unattended_close_allowed"]
    device = Device.parse("test-account", door_payload)
    assert device.can_move("open") and device.can_move("close")


@pytest.mark.parametrize(
    "field", ["monitor_only_mode", "is_unattended_close_allowed", "is_unattended_open_allowed"]
)
def test_null_flags_read_as_the_app_defaults(door_payload, field):
    # JSONObject.optBoolean returns the default for null.
    door_payload["state"][field] = None
    device = Device.parse("test-account", door_payload)
    assert device.can_move("open") and device.can_move("close")


@pytest.mark.parametrize("action", ["open", "close"])
@pytest.mark.parametrize("value", [False, "true", 1])
def test_explicit_or_malformed_unattended_flags_deny(door_payload, action, value):
    door_payload["state"][f"is_unattended_{action}_allowed"] = value
    assert not Device.parse("test-account", door_payload).can_move(action)


@pytest.mark.parametrize(
    "state,allowed",
    [
        # VirtualGarageDoorDevice.v1: a fault blocks only while a sensor is learned.
        ({"sensor_comm_error": True, "number_of_learned_dps_devices": 1}, False),
        ({"sensor_comm_error": True}, True),
        ({"sensor_comm_error": True, "number_of_learned_dps_devices": 0}, True),
        ({"sensor_comm_error": False, "number_of_learned_dps_devices": 2}, True),
        ({"sensor_comm_error": "yes", "number_of_learned_dps_devices": 1}, False),
    ],
)
def test_virtual_opener_with_a_faulty_learned_sensor_cannot_move(door_payload, state, allowed):
    door_payload["device_type"] = "virtualgaragedooropener"
    door_payload["state"].update(monitor_only_mode=False, **state)
    device = Device.parse("test-account", door_payload)
    assert device.can_move("close") is allowed


@pytest.mark.parametrize(
    "state",
    [
        {"attached_work_light_error_present": True},
        {"failed_credential_lockout_active": True},
        {"monitor_only_mode": "false"},
    ],
)
def test_app_precheck_errors_block_door_commands(door_payload, state):
    door_payload["state"].update(state)
    device = Device.parse("test-account", door_payload)
    assert not device.can_move("open") and not device.can_move("close")
    with pytest.raises(CommandRejected):
        device.validate_command("close")


def test_virtual_opener_requires_explicit_exit_from_monitor_mode(door_payload):
    door_payload["device_type"] = "virtualgaragedooropener"
    del door_payload["state"]["monitor_only_mode"]
    device = Device.parse("test-account", door_payload)
    assert not device.can_move("open") and not device.can_move("close")
    door_payload["state"]["monitor_only_mode"] = False
    assert Device.parse("test-account", door_payload).can_move("close")


@pytest.mark.parametrize("value", [None, "moving", "unknown", 1, [], {}])
def test_unknown_or_malformed_door_state_remains_unknown(door_payload, value):
    door_payload["state"]["door_state"] = value
    assert Device.parse("test-account", door_payload).door_state is None


@pytest.mark.parametrize("value", ["OPEN", "Closed", "OPENING", "Closing", "STOPPED"])
def test_known_door_states_are_case_insensitive(door_payload, value):
    door_payload["state"]["door_state"] = value
    assert Device.parse("test-account", door_payload).door_state == value.lower()


async def test_overlapping_door_commands_are_rejected_without_queuing_a_second_action(tokens, door):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def hold_request(*args, **kwargs):
        started.set()
        await finish.wait()
        return Response(204, {}, b"")

    client = Client(transport=AsyncMock(), tokens=tokens)
    client.http.request.side_effect = hold_request
    first = asyncio.create_task(client.command(door, "open"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(CommandRejected, match="already in progress"):
            await client.command(door, "close")
        assert client.http.request.await_count == 1
    finally:
        finish.set()
        await first
    assert client.http.request.await_count == 1


async def test_cancelled_door_command_is_not_replayed(tokens, door):
    started = asyncio.Event()

    async def hold_request(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    client = Client(transport=AsyncMock(), tokens=tokens)
    client.http.request.side_effect = hold_request
    pending = asyncio.create_task(client.command(door, "open"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert client.http.request.await_count == 1


async def test_server_error_after_door_command_reports_uncertain_without_replay(tokens, door):
    client = Client(transport=AsyncMock(), tokens=tokens)
    client.http.request.return_value = Response(503, {}, b"private server error")
    with pytest.raises(CommandUncertain, match="outcome is unknown"):
        await client.command(door, "close")
    client.http.request.assert_awaited_once()


async def test_discovery_covers_multiple_accounts(tokens, door_payload):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(
        side_effect=[
            json_response({"accounts": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]}),
            json_response({"count": 1, "items": [door_payload]}),
            json_response({"count": 1, "items": [door_payload]}),
        ]
    )
    devices = await client.discover()
    assert set(devices) == {"a:TEST-GDO-1", "b:TEST-GDO-1"}


async def test_partial_discovery_is_not_silently_accepted(tokens):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(return_value=json_response({"count": 2, "items": []}))
    with pytest.raises(ProtocolError):
        await client.devices("test")


async def test_camera_snapshot_uses_observed_host_and_has_no_stream_guess(camera_device):
    client = MagicMock()
    client.request = AsyncMock(return_value=Response(200, {}, b"\xff\xd8\xfftest"))
    assert await Video(client).snapshot(camera_device) == b"\xff\xd8\xfftest"
    args = client.request.call_args
    assert args.args[1:] == (
        "https://media.hubs.tend-us.tendplatform.com",
        "/cxs/api/devices/CAM123/cam/recentImage.jpg",
    )
    assert args.kwargs["headers"] == {"AntiCSRF": "AntiCSRF"}
    assert args.kwargs["params"] == {"size": "HD", "forceRefresh": "true"}


async def test_camera_setting_is_partial_and_validates_capabilities(
    camera_device, camera_details, tokens
):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(
        side_effect=[json_response(camera_details), Response(204, {}, b"")]
    )
    video = Video(client)
    await video.setting(camera_device, "mic", "enabled", False)
    assert client.http.request.call_args.kwargs["json"] == {"mic": {"enabled": False}}
    client.http.request.reset_mock(side_effect=True)
    client.http.request.return_value = json_response(camera_details)
    with pytest.raises(CommandRejected):
        await video.setting(camera_device, "spk", "vol", 100)
    assert client.http.request.await_count == 1


@pytest.mark.parametrize(
    "value,expected",
    [("15", 15), (None, 60), ("bad", 60), ("nan", 60), ("0", 60), ("172800", 86400)],
)
def test_retry_after(value, expected):
    assert retry_seconds(value) == expected


async def test_cooldown_prevents_network_request():
    session = MagicMock()
    http = Http(session)
    http.backoff.record(ACCOUNTS, RateLimited(30))
    with pytest.raises(RateLimited):
        await http.request("GET", ACCOUNTS)
    session.request.assert_not_called()


async def test_access_denied_is_distinct_from_bad_password(tokens):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(return_value=Response(403, {}, b"secret server data"))
    with pytest.raises(AccessDenied) as error:
        await client.accounts()
    assert "secret" not in str(error.value)


async def test_removed_device_command_is_rejected_before_network(tokens):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock()
    with pytest.raises(CommandRejected):
        await client.command(None, "open")
    client.http.request.assert_not_awaited()


async def test_lamp_command_does_not_assume_opener_light_is_a_standalone_lamp(tokens, door):
    client = Client(MagicMock(), tokens)
    client.http.request = AsyncMock(return_value=Response(204, {}, b""))
    with pytest.raises(CommandRejected):
        await client.lamp(door, True)
    client.http.request.assert_not_awaited()
    lamp = Device.parse(
        "account",
        {
            "serial_number": "LAMP",
            "device_type": "lamp",
            "state": {"online": True},
        },
    )
    await client.lamp(lamp, True)
    assert client.http.request.call_args.args == (
        "PUT",
        "https://account-devices-lamp.myq-cloud.com/api/v6.0/accounts/account/lamps/LAMP/on",
    )
    assert "json" not in client.http.request.call_args.kwargs


async def test_null_camera_sections_are_absent(camera_device, camera_details):
    camera_details["cap"]["mic"] = None
    camera_details["settings"]["mic"] = None
    client = MagicMock()
    client.request = AsyncMock(return_value=json_response(camera_details))
    details = await Video(client).details(camera_device)
    assert "mic" not in details["cap"] and "mic" not in details["settings"]


async def test_malformed_camera_capabilities_are_rejected(camera_device, camera_details):
    camera_details["cap"]["spk"] = []
    client = MagicMock()
    client.request = AsyncMock(return_value=json_response(camera_details))
    with pytest.raises(ProtocolError):
        await Video(client).details(camera_device)


def test_nullable_device_fields_do_not_break_discovery_or_grant_permissions(door_payload):
    door_payload.update(state=None, capabilities=None)
    device = Device.parse("test-account", door_payload)
    assert device.capabilities == frozenset()
    # A null state object takes the app defaults: online, not monitor-only, and
    # unattended operation allowed. The position remains unknown.
    assert device.online and device.door_state is None
    assert device.can_move("open") and device.can_move("close")


def test_door_action_requires_known_family(door_payload):
    door_payload["device_family"] = "unknown"
    device = Device.parse("test-account", door_payload)
    assert not device.can_move("open")


def test_repeated_rate_limits_without_retry_after_back_off_and_reset():
    from custom_components.myq.api.backoff import ServiceBackoff

    backoff, url = ServiceBackoff(), "https://devices.myq-cloud.com/api"
    waits = []
    for _ in range(4):
        backoff.record(url, RateLimited(60))
        with pytest.raises(RateLimited) as limited:
            backoff.check(url)
        waits.append(round(limited.value.retry_after, -1))
    assert waits == [60, 120, 240, 480]
    backoff.succeeded(url)
    backoff._until.clear()
    backoff.record(url, RateLimited(60))
    with pytest.raises(RateLimited) as limited:
        backoff.check(url)
    assert round(limited.value.retry_after, -1) == 60
    # A server's own Retry-After is followed as given.
    backoff.record("https://alerts.myq-cloud.com/x", RateLimited(5, server_retry_after=True))
    with pytest.raises(RateLimited) as limited:
        backoff.check("https://alerts.myq-cloud.com/y")
    assert limited.value.retry_after <= 5

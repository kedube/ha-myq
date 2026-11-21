"""Synthetic opener settings; never send live garage commands."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.client import Client
from custom_components.myq.api.const import GDO, NATIVE_HEADERS
from custom_components.myq.api.errors import (
    AuthenticationError,
    CannotConnect,
    CommandRejected,
    CommandUncertain,
    RateLimited,
    RequestUnauthorized,
)
from custom_components.myq.api.garage import GarageSettings
from custom_components.myq.api.http import Response
from custom_components.myq.garage_entities import (
    garage_number_entities,
    garage_select_entities,
    garage_switch_entities,
)

from .conftest import json_response


def owner(account_id="test-account", role="owner"):
    return AccountAccess(account_id, role, {"devices": frozenset({"All"})})


def garage_client(http, tokens, access=None):
    async def owner_access(account_id):
        return access or owner(account_id)

    return GarageSettings(Client(transport=http, tokens=tokens), owner_access)


async def test_reads_typed_settings_and_caches_optional_support(http, tokens, door):
    http.request.side_effect = [
        json_response({"light_timer_setting": 150}),
        json_response({"time_to_close_setting": 300}),
        json_response({"enable_pir_control": True}),
    ]
    garage = garage_client(http, tokens)
    expected = {
        "light_timer_setting": 150,
        "time_to_close_setting": 300,
        "enable_pir_control": True,
        "enable_motion_wl": False,
    }
    assert await garage.read(door) == expected
    assert await garage.read(door) == expected
    assert http.request.await_count == 3
    for call, endpoint in zip(
        http.request.await_args_list,
        ("lighttimer", "timetoclose", "worklight_settings"),
        strict=True,
    ):
        assert call.args == (
            "GET",
            f"{GDO}/api/v6.0/accounts/test-account/door_openers/TEST-GDO-1/{endpoint}",
        )
        assert call.kwargs["headers"] == {**NATIVE_HEADERS, "Authorization": "Bearer test-access"}


async def test_unsupported_endpoints_cached_but_capability_change_rediscovers(http, tokens, door):
    http.request.side_effect = [Response(404, {}, b"")] * 6
    garage = garage_client(http, tokens)
    assert await garage.read(door) == {}
    assert await garage.read(door) == {}
    assert http.request.await_count == 3
    assert await garage.read(replace(door, capabilities=frozenset({"new-capability"}))) == {}
    assert http.request.await_count == 6


async def test_transient_endpoint_failure_does_not_hide_healthy_siblings(http, tokens, door):
    http.request.side_effect = [
        CannotConnect("Synthetic failure"),
        json_response({"time_to_close_setting": 0}),
        json_response({}),
        json_response({"light_timer_setting": 270}),
    ]
    garage = garage_client(http, tokens)
    assert await garage.read(door) == {"time_to_close_setting": 0}
    assert await garage.read(door) == {"light_timer_setting": 270, "time_to_close_setting": 0}
    assert http.request.await_count == 4


@pytest.mark.parametrize("error", [AuthenticationError("Synthetic failure"), RateLimited()])
async def test_authentication_and_throttling_are_not_swallowed(http, tokens, door, error):
    http.request.side_effect = error
    with pytest.raises(type(error)):
        await garage_client(http, tokens).read(door)
    http.request.assert_awaited_once()


@pytest.mark.parametrize(
    "kind", ["gate", "commercialdooropener", "virtualgaragedooropener", "lamp"]
)
async def test_unverified_families_are_not_probed(http, tokens, door, kind):
    assert await garage_client(http, tokens).read(replace(door, device_type=kind)) == {}
    http.request.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [
        {"light_timer_setting": True},
        {"light_timer_setting": 120},
        {"time_to_close_setting": 30},
        {"time_to_close_setting": 6000},
        {"enable_pir_control": "true"},
        {"enable_pir_control": None, "enable_motion_wl": None},
        [],
    ],
)
async def test_unknown_or_malformed_settings_grant_no_control(http, tokens, door, response):
    http.request.return_value = json_response(response)
    assert await garage_client(http, tokens).read(door) == {}


@pytest.mark.parametrize(
    "method,value,endpoint,current,body",
    [
        (
            "set_light_timer",
            210,
            "lighttimer",
            {"light_timer_setting": 90},
            {"light_timer_setting": 210},
        ),
        (
            "set_time_to_close",
            99,
            "timetoclose",
            {"time_to_close_setting": 0},
            {"time_to_close_setting": 5940},
        ),
        (
            "set_time_to_close",
            0,
            "timetoclose",
            {"time_to_close_setting": 300},
            {"time_to_close_setting": 0},
        ),
        (
            "set_motion_activation",
            False,
            "worklight_settings",
            {"enable_pir_control": True, "enable_motion_wl": False},
            {"enable_pir_control": False, "enable_motion_wl": False},
        ),
    ],
)
async def test_writes_exact_native_body_after_fresh_support_read(
    http, tokens, door, method, value, endpoint, current, body
):
    http.request.side_effect = [json_response(current), Response(204, {}, b"")]
    await getattr(garage_client(http, tokens), method)(door, value)
    assert http.request.await_count == 2
    call = http.request.await_args_list[-1]
    assert call.args == (
        "PUT",
        f"{GDO}/api/v6.0/accounts/test-account/door_openers/TEST-GDO-1/{endpoint}",
    )
    assert call.kwargs["json"] == body


@pytest.mark.parametrize("status,error", [(401, RequestUnauthorized), (503, CommandUncertain)])
async def test_mutation_never_replayed_on_refused_or_uncertain_response(
    http, tokens, door, status, error
):
    # A 401 with a code other than 401.102/401 is not renewed (ft0/c1), so not resent.
    http.request.side_effect = [
        json_response({"light_timer_setting": 90}),
        Response(status, {}, b'{"code": "401.122"}' if status == 401 else b""),
    ]
    with pytest.raises(error):
        await garage_client(http, tokens).set_light_timer(door, 150)
    assert [call.args[0] for call in http.request.await_args_list] == ["GET", "PUT"]


@pytest.mark.parametrize(
    "method,value",
    [
        ("set_light_timer", 120),
        ("set_light_timer", True),
        ("set_time_to_close", -1),
        ("set_time_to_close", 100),
        ("set_time_to_close", 1.5),
        ("set_time_to_close", True),
        ("set_motion_activation", 1),
    ],
)
async def test_invalid_writes_are_rejected_locally(http, tokens, door, method, value):
    with pytest.raises(CommandRejected):
        await getattr(garage_client(http, tokens), method)(door, value)
    http.request.assert_not_awaited()


@pytest.mark.parametrize("state", [{"monitor_only_mode": True}, {"online": False}])
async def test_settings_recheck_monitor_mode_and_online_before_network(http, tokens, door, state):
    device = replace(door, state={**door.state, **state})
    with pytest.raises(CommandRejected):
        await garage_client(http, tokens).set_light_timer(device, 150)
    http.request.assert_not_awaited()


async def test_time_to_close_requires_explicit_close_permission(http, tokens, door):
    device = replace(door, state={**door.state, "is_unattended_close_allowed": False})
    with pytest.raises(CommandRejected):
        await garage_client(http, tokens).set_time_to_close(device, 5)
    http.request.assert_not_awaited()


async def test_cached_support_is_not_write_authorization(http, tokens, door):
    http.request.side_effect = [
        json_response({"light_timer_setting": 90}),
        json_response({}),
        json_response({}),
        Response(404, {}, b""),
    ]
    garage = garage_client(http, tokens)
    assert await garage.read(door) == {"light_timer_setting": 90}
    with pytest.raises(CommandRejected):
        await garage.set_light_timer(door, 150)
    assert all(call.args[0] == "GET" for call in http.request.await_args_list)


async def test_successful_write_invalidates_only_its_read_cache(http, tokens, door):
    http.request.side_effect = [
        json_response({"light_timer_setting": 90}),
        json_response({}),
        json_response({}),
        json_response({"light_timer_setting": 90}),
        Response(204, {}, b""),
        json_response({"light_timer_setting": 90}),
    ]
    garage = garage_client(http, tokens)
    assert await garage.read(door) == {"light_timer_setting": 90}
    await garage.set_light_timer(door, 270)
    # An accepted write never optimistically reports the requested value.
    assert await garage.read(door) == {"light_timer_setting": 90}
    assert http.request.await_count == 6


@pytest.mark.parametrize("read_timing", ["before_write", "during_write"])
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("old_status", [200, 404])
async def test_late_read_cannot_restore_cache_after_mutation(
    http, tokens, door, read_timing, uncertain, old_status
):
    old_started, release_old = asyncio.Event(), asyncio.Event()
    write_started, release_write = asyncio.Event(), asyncio.Event()
    server_value = 90
    delay_next_read = read_timing == "before_write"

    async def send(method, url, **kwargs):
        nonlocal server_value, delay_next_read
        if method == "PUT":
            write_started.set()
            await release_write.wait()
            server_value = kwargs["json"]["light_timer_setting"]
            if uncertain:
                raise CannotConnect("Synthetic lost acknowledgement")
            return Response(204, {}, b"")
        captured = server_value
        if delay_next_read:
            delay_next_read = False
            old_started.set()
            await release_old.wait()
            if old_status == 404:
                return Response(404, {}, b"")
        return json_response({"light_timer_setting": captured})

    http.request.side_effect = send
    garage = garage_client(http, tokens)
    old_read = write = None
    try:
        if read_timing == "before_write":
            old_read = asyncio.create_task(garage._read(door, "lighttimer", force=True))
            await asyncio.wait_for(old_started.wait(), 1)
        write = asyncio.create_task(garage.set_light_timer(door, 150))
        await asyncio.wait_for(write_started.wait(), 1)
        if read_timing == "during_write":
            delay_next_read = True
            old_read = asyncio.create_task(garage._read(door, "lighttimer", force=True))
            await asyncio.wait_for(old_started.wait(), 1)
        release_write.set()
        if uncertain:
            with pytest.raises(CommandUncertain):
                await write
        else:
            await write
        release_old.set()
        await old_read
        assert await garage._read(door, "lighttimer") == {"light_timer_setting": 150}
        reads = http.request.await_count
        assert await garage._read(door, "lighttimer") == {"light_timer_setting": 150}
        assert http.request.await_count == reads
        assert sum(c.args[0] == "PUT" for c in http.request.await_args_list) == 1
    finally:
        release_old.set()
        release_write.set()
        await asyncio.gather(
            *(task for task in (old_read, write) if task is not None), return_exceptions=True
        )


async def test_cache_expires_for_external_native_app_changes(http, tokens, door):
    http.request.side_effect = [
        json_response({"light_timer_setting": 90}),
        json_response({}),
        json_response({}),
        json_response({"light_timer_setting": 270}),
    ]
    garage = garage_client(http, tokens)
    with patch("custom_components.myq.api.garage.time.monotonic", return_value=100):
        assert await garage.read(door) == {"light_timer_setting": 90}
    # The app reads these only on its settings screens; HA re-reads them every 30 minutes.
    with patch("custom_components.myq.api.garage.time.monotonic", return_value=1801):
        assert await garage.read(door) == {"light_timer_setting": 90}
    with patch("custom_components.myq.api.garage.time.monotonic", return_value=1901):
        assert await garage.read(door) == {"light_timer_setting": 270}


def test_entities_follow_observed_values_and_permission_revocation(door):
    data = SimpleNamespace(
        devices={door.key: door},
        garage_settings={
            door.key: {
                "light_timer_setting": 210,
                "time_to_close_setting": 300,
                "enable_pir_control": False,
                "enable_motion_wl": True,
            }
        },
        account_access={"test-account": owner()},
    )
    coordinator = SimpleNamespace(data=data, last_update_success=True)
    select = next(garage_select_entities(coordinator, door))
    number = next(garage_number_entities(coordinator, door))
    switch = next(garage_switch_entities(coordinator, door))
    assert select.current_option == "210"
    assert number.native_value == 5
    assert switch.is_on is True
    assert all(entity.available for entity in (select, number, switch))
    data.devices[door.key] = replace(door, state={**door.state, "monitor_only_mode": True})
    assert not any(entity.available for entity in (select, number, switch))
    data.devices[door.key] = door
    # GdoDeviceSettingsProvider: opener settings are for the account owner only.
    data.account_access = {"test-account": owner(role="co_owner")}
    assert not any(entity.available for entity in (select, number, switch))
    data.account_access = {"test-account": owner()}
    data.garage_settings.clear()
    assert not any(entity.available for entity in (select, number, switch))


async def test_non_owner_writes_send_nothing(http, tokens, door):
    garage = garage_client(http, tokens, owner(role="co_owner"))
    with pytest.raises(CommandRejected):
        await garage.set_light_timer(door, 210)
    http.request.assert_not_awaited()


async def test_entity_passes_minutes_and_toggles_both_worklight_fields_through_api(door):
    data = SimpleNamespace(
        devices={door.key: door},
        garage_settings={
            door.key: {
                "time_to_close_setting": 300,
                "enable_pir_control": False,
                "enable_motion_wl": False,
            }
        },
        account_access={"test-account": owner()},
    )
    garage = SimpleNamespace(set_time_to_close=AsyncMock(), set_motion_activation=AsyncMock())

    async def execute(operation):
        await operation

    coordinator = SimpleNamespace(
        data=data, last_update_success=True, garage=garage, execute=execute
    )
    await next(garage_number_entities(coordinator, door)).async_set_native_value(99)
    await next(garage_switch_entities(coordinator, door)).async_turn_on()
    garage.set_time_to_close.assert_awaited_once_with(door, 99)
    garage.set_motion_activation.assert_awaited_once_with(door, True)

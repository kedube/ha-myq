"""Native thermostat payloads, access gates and real HA service routing."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.myq.api import climate as climate_api
from custom_components.myq.api.client import Client
from custom_components.myq.api.climate import Thermostats
from custom_components.myq.api.errors import (
    CannotConnect,
    CommandRejected,
    CommandUncertain,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.models import Device
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import Snapshot


@pytest.fixture
def payload():
    return {
        "serial_number": "HVAC/1",
        "name": "Hall thermostat",
        "device_type": "vendor_thermostat",
        "device_family": "hvacs",
        "state": {
            "online": True,
            "is_managed": True,
            "temperature_unit": "celsius",
            "current_temperature_celsius": 21.2,
            "relative_humidity": 45,
            "current_climate_setting": {
                "hvac_mode_setting": "heat_cool",
                "fan_mode_setting": "auto",
                "heating_set_point_celsius": 20.0,
                "cooling_set_point_celsius": 25.0,
            },
        },
        "specifications": {
            "available_hvac_mode_settings": ["off", "heat", "cool", "heat_cool"],
            "available_fan_mode_settings": ["auto", "on", "circulate"],
            "min_heating_set_point_celsius": 10.0,
            "max_heating_set_point_celsius": 30.0,
            "min_cooling_set_point_celsius": 15.0,
            "max_cooling_set_point_celsius": 35.0,
        },
    }


@pytest.fixture
def client(http, tokens):
    http.request.return_value = Response(204, {}, b"")
    return Client(transport=http, tokens=tokens)


@pytest.mark.parametrize(
    "mode,points",
    [
        ("heat", {"heating_set_point": 20.0}),
        ("cool", {"cooling_set_point": 25.0}),
        ("heat_cool", {"heating_set_point": 20.0, "cooling_set_point": 25.0}),
        ("off", {}),
    ],
)
async def test_mode_exact_wire(client, payload, mode, points):
    await Thermostats(client).set_mode(Device.parse("account/1", payload), mode)
    request = client.http.request
    request.assert_awaited_once()
    assert request.call_args.args == (
        "PUT",
        "https://devices-action.myq-cloud.com/api/v1.0/accounts/account%2F1/devices/HVAC%2F1",
    )
    assert request.call_args.kwargs["json"] == {
        "action": mode,
        "device_family": "hvacs",
        "temperature_unit": "celsius",
        **points,
        # t60/i0.d0: the control screen's save carries the fan with the mode.
        "fan_mode": "auto",
    }
    assert "trigger" not in request.call_args.kwargs["json"]


async def test_fan_exact_wire(client, payload):
    # The app has no caller for set_fan_mode: the fan goes inside the current mode's action.
    await Thermostats(client).set_fan(Device.parse("a", payload), "circulate")
    assert client.http.request.call_args.kwargs["json"] == {
        "action": "heat_cool",
        "device_family": "hvacs",
        "temperature_unit": "celsius",
        "heating_set_point": 20.0,
        "cooling_set_point": 25.0,
        "fan_mode": "circulate",
    }


@pytest.mark.parametrize(
    "state",
    [
        {"online": False},
        {"online": "true"},
        {"is_managed": False},
        {"is_managed": None},
        {"monitor_only_mode": True},
        {"monitor_only_mode": 0},
        {"temperature_unit": "F"},
    ],
)
async def test_access_denial_sends_nothing(client, payload, state):
    payload["state"].update(state)
    with pytest.raises(CommandRejected):
        await Thermostats(client).set_mode(Device.parse("a", payload), "off")
    client.http.request.assert_not_awaited()


@pytest.mark.parametrize("value", [True, "20", float("inf"), float("nan"), 9.0, 30.1])
async def test_invalid_temperature_never_sent(client, payload, value):
    with pytest.raises(CommandRejected):
        await Thermostats(client).set_mode(Device.parse("a", payload), "heat", heating=value)
    client.http.request.assert_not_awaited()


async def test_threshold_is_an_alert_band_and_deadband_still_applies(client, payload):
    # HvacDevice takes setpoint bounds from the min/max specifications only.
    payload["specifications"]["temperature_threshold"] = {
        "enabled": True,
        "lower_limit_celsius": 19.0,
        "upper_limit_celsius": 26.0,
    }
    thermostat = Device.parse("a", payload)
    api = Thermostats(client)
    with pytest.raises(CommandRejected):
        await api.set_mode(thermostat, "heat_cool", heating=22.5, cooling=25.0)
    client.http.request.assert_not_awaited()
    await api.set_mode(thermostat, "heat", heating=18.5)
    assert client.http.request.call_args.kwargs["json"]["heating_set_point"] == 18.5
    assert climate_api.threshold(thermostat) == {
        "enabled": True,
        "lower_limit_celsius": 19.0,
        "upper_limit_celsius": 26.0,
    }


async def test_uppercase_enum_names_are_read_and_lowercase_is_written(client, payload):
    # Gson's default enum adapter reads constant names; the app writes mwString.
    payload["state"]["temperature_unit"] = "CELSIUS"
    payload["state"]["current_climate_setting"]["hvac_mode_setting"] = "HEAT_COOL"
    payload["specifications"]["available_hvac_mode_settings"] = ["OFF", "HEAT", "HEAT_COOL"]
    payload["specifications"]["available_fan_mode_settings"] = ["AUTO", "ON"]
    thermostat = Device.parse("a", payload)
    assert climate_api.unit(thermostat) == "celsius"
    assert climate_api.modes(thermostat) == ["off", "heat", "heat_cool"]
    assert climate_api.setting(thermostat, "hvac_mode_setting") == "heat_cool"
    await Thermostats(client).set_fan(thermostat, "on")
    assert client.http.request.call_args.kwargs["json"]["fan_mode"] == "on"
    await Thermostats(client).set_mode(thermostat, "heat", heating=21.0)
    assert client.http.request.call_args.kwargs["json"]["temperature_unit"] == "celsius"


async def test_absent_unit_and_managed_flag_use_the_app_defaults(client, payload):
    del payload["state"]["is_managed"], payload["state"]["temperature_unit"]
    payload["state"]["current_temperature_fahrenheit"] = 70
    payload["specifications"].update(
        {"min_heating_set_point_fahrenheit": 50, "max_heating_set_point_fahrenheit": 86}
    )
    payload["state"]["current_climate_setting"]["manual_override_allowed"] = False
    thermostat = Device.parse("a", payload)
    assert climate_api.unit(thermostat) == "fahrenheit"
    assert climate_api.can_control(thermostat)
    await Thermostats(client).set_mode(thermostat, "heat", heating=68)
    assert client.http.request.call_args.kwargs["json"]["temperature_unit"] == "fahrenheit"


async def test_missing_limits_or_mode_does_not_invent_defaults(client, payload):
    payload["specifications"] = {"available_hvac_mode_settings": ["heat", "off"]}
    api, thermostat = Thermostats(client), Device.parse("a", payload)
    with pytest.raises(CommandRejected):
        await api.set_mode(thermostat, "heat")
    with pytest.raises(CommandRejected):
        await api.set_mode(thermostat, "cool")
    with pytest.raises(CommandRejected):
        await api.set_fan(thermostat, "on")
    client.http.request.assert_not_awaited()
    await api.set_mode(thermostat, "off")


async def test_fahrenheit_rounds_to_whole_points_and_keeps_five_degree_gap(client, payload):
    payload["state"]["temperature_unit"] = "fahrenheit"
    payload["specifications"].update(
        {
            "min_heating_set_point_fahrenheit": 50,
            "max_heating_set_point_fahrenheit": 86,
            "min_cooling_set_point_fahrenheit": 59,
            "max_cooling_set_point_fahrenheit": 95,
        }
    )
    api, thermostat = Thermostats(client), Device.parse("a", payload)
    with pytest.raises(CommandRejected, match="too close"):
        await api.set_mode(thermostat, "heat_cool", heating=74, cooling=78)
    client.http.request.assert_not_awaited()
    # HvacSetting rounds half up to whole degrees Fahrenheit before sending.
    await api.set_mode(thermostat, "heat_cool", heating=70.5, cooling=77.4)
    sent = client.http.request.call_args.kwargs["json"]
    assert (sent["heating_set_point"], sent["cooling_set_point"]) == (71.0, 77.0)
    # HA converts 21 C to 69.8 F for a Fahrenheit thermostat; it is rounded, not refused.
    await api.set_mode(thermostat, "heat", heating=69.8)
    assert client.http.request.call_args.kwargs["json"]["heating_set_point"] == 70.0


async def test_celsius_rounds_to_tenths_and_exact_gap_is_allowed(client, payload):
    api, thermostat = Thermostats(client), Device.parse("a", payload)
    await api.set_mode(thermostat, "heat", heating=20.55)
    assert client.http.request.call_args.kwargs["json"]["heating_set_point"] == 20.6
    # 18.9 - 15.9 is just under 3 in binary floating point; the app's gap is exact.
    await api.set_mode(thermostat, "heat_cool", heating=15.9, cooling=18.9)
    sent = client.http.request.call_args.kwargs["json"]
    assert (sent["heating_set_point"], sent["cooling_set_point"]) == (15.9, 18.9)


async def test_one_changed_target_moves_the_other_to_keep_the_gap(client, payload):
    api, thermostat = Thermostats(client), Device.parse("a", payload)
    # HvacSettingUpdater: raising heat to 24 pushes cool from 25 to 27.
    await api.set_mode(thermostat, "heat_cool", heating=24)
    sent = client.http.request.call_args.kwargs["json"]
    assert (sent["heating_set_point"], sent["cooling_set_point"]) == (24.0, 27.0)
    # Lowering cool to 18 pulls heat down to 15.
    await api.set_mode(thermostat, "heat_cool", cooling=18)
    sent = client.http.request.call_args.kwargs["json"]
    assert (sent["heating_set_point"], sent["cooling_set_point"]) == (15.0, 18.0)


async def test_missing_target_is_derived_like_the_app(client, payload):
    payload["state"]["current_climate_setting"] = {"hvac_mode_setting": "heat"}
    api, thermostat = Thermostats(client), Device.parse("a", payload)
    # HvacSetting.Companion.c: neither reported, so 20/26 C.
    await api.set_mode(thermostat, "heat_cool")
    sent = client.http.request.call_args.kwargs["json"]
    assert (sent["heating_set_point"], sent["cooling_set_point"]) == (20.0, 26.0)
    payload["state"]["current_climate_setting"]["heating_set_point_celsius"] = 21.0
    await Thermostats(client).set_mode(Device.parse("a", payload), "cool")
    assert client.http.request.call_args.kwargs["json"]["cooling_set_point"] == 24.0


async def test_next_write_uses_the_mode_just_sent_not_a_stale_readback(client, payload):
    api, thermostat = Thermostats(client), Device.parse("a", payload)
    await api.set_mode(thermostat, "cool", cooling=24)
    # The readback still says heat_cool; a fan change must not send heat_cool back.
    await api.set_fan(thermostat, "on")
    sent = client.http.request.call_args.kwargs["json"]
    assert sent["action"] == "cool" and sent["cooling_set_point"] == 24.0
    await api.set_temperature(thermostat, temperature=23)
    sent = client.http.request.call_args.kwargs["json"]
    assert sent == {
        "action": "cool",
        "device_family": "hvacs",
        "temperature_unit": "celsius",
        "cooling_set_point": 23.0,
        "fan_mode": "on",
    }


async def test_off_is_offered_even_when_not_listed(client, payload):
    # The app's on/off toggle is always shown; only heat/cool/auto follow the list.
    payload["specifications"]["available_hvac_mode_settings"] = ["heat", "cool"]
    thermostat = Device.parse("a", payload)
    assert climate_api.modes(thermostat) == ["off", "heat", "cool"]
    await Thermostats(client).set_mode(thermostat, "off")
    assert client.http.request.call_args.kwargs["json"]["action"] == "off"


async def test_uncertain_mutation_not_replayed(client, payload):
    client.http.request.side_effect = CannotConnect("timeout")
    with pytest.raises(CommandUncertain):
        await Thermostats(client).set_mode(Device.parse("a", payload), "off")
    client.http.request.assert_awaited_once()


async def test_overlapping_thermostat_actions_rejected_and_cancellation_releases(client, payload):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await finish.wait()
        return Response(204, {}, b"")

    client.http.request.side_effect = wait
    api, thermostat = Thermostats(client), Device.parse("a", payload)
    task = asyncio.create_task(api.set_mode(thermostat, "off"))
    await entered.wait()
    with pytest.raises(CommandRejected, match="already in progress"):
        await api.set_fan(thermostat, "on")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    await api.set_fan(thermostat, "on")
    assert client.http.request.await_count == 2


async def test_ha_climate_services_and_permission_change(
    hass, enable_custom_integrations, tokens, payload
):
    thermostat = Device.parse("a", payload)
    entry = MockConfigEntry(domain=DOMAIN, data={"tokens": tokens.as_dict()})
    entry.add_to_hass(hass)
    with patch.object(Client, "discover", AsyncMock(return_value={thermostat.key: thermostat})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity = hass.states.async_all("climate")[0]
    assert entity.state == "heat_cool"
    assert entity.attributes["current_temperature"] == 21.2
    assert entity.attributes["target_temp_low"] == 20
    coordinator = entry.runtime_data
    with (
        patch.object(
            coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
        ) as request,
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh,
    ):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity.entity_id, "target_temp_low": 21, "target_temp_high": 25},
            blocking=True,
        )
        assert request.call_args.kwargs["json"]["heating_set_point"] == 21
        # t60/i0.e0: the app re-reads two seconds after the change, not at once.
        refresh.assert_not_awaited()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
        await hass.async_block_till_done()
        refresh.assert_awaited_once()
        assert hass.states.get(entity.entity_id).attributes["target_temp_low"] == 20
        restricted = deepcopy(payload)
        restricted["state"]["is_managed"] = False
        coordinator.async_set_updated_data(
            Snapshot({thermostat.key: Device.parse("a", restricted)})
        )
        await hass.async_block_till_done()
        request.reset_mock()
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity.entity_id, "hvac_mode": "off"},
                blocking=True,
            )
        request.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_out_of_range_temperature_rounds_to_nothing():
    from custom_components.myq.api.climate import _rounded

    assert _rounded(1e30, "fahrenheit") is None
    assert str(_rounded(71.5, "fahrenheit")) == "72"

"""Entity actions with no prior coverage: lamps, lock management and thermostats."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.myq.api.account_access import parse_account_access
from custom_components.myq.api.const import LAMPS
from custom_components.myq.api.http import Response
from custom_components.myq.api.models import Device
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import FAST_POLL_SECONDS, Snapshot

from .ha import MockConfigEntry
from .test_locks import make_lock
from .test_management import access_payload, modern_lock


@pytest.fixture(autouse=True)
def allow_custom(enable_custom_integrations):
    yield


@pytest.fixture
def load(hass, tokens, monkeypatch):
    """Set up HA with these devices; discovery and grants stay synthetic for the test."""

    async def load(*devices, access=None):
        entry = MockConfigEntry(domain=DOMAIN, unique_id="u", data={"tokens": tokens.as_dict()})
        entry.add_to_hass(hass)
        grants = parse_account_access(access_payload()) if access is None else access
        monkeypatch.setattr(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={device.key: device for device in devices}),
        )
        monkeypatch.setattr(
            "custom_components.myq.api.client.Client.devices",
            AsyncMock(side_effect=lambda account: [d for d in devices if d.account_id == account]),
        )
        monkeypatch.setattr(
            "custom_components.myq.api.management.Management.account_access",
            AsyncMock(return_value=grants),
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry.runtime_data

    return load


def sent(request) -> dict:
    """Body of the single PUT; reads (the follow-up refresh) are ignored."""
    (put,) = [call for call in request.call_args_list if call.args[0] == "PUT"]
    return put.kwargs.get("json", {})


def entity_id(hass, domain, suffix):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    return next(
        item.entity_id
        for item in registry.entities.values()
        if item.domain == domain and item.unique_id.endswith(":" + suffix)
    )


def lamp(state="off", **extra):
    return Device.parse(
        "test-account",
        {
            "serial_number": "LAMP-1",
            "name": "Porch lamp",
            "device_type": "lamp",
            "device_family": "lamp",
            "state": {"online": True, "lamp_state": state, **extra},
        },
    )


@pytest.mark.parametrize("reported,expected", [("on", "on"), ("OFF", "off"), ("dim", "unknown")])
async def test_lamp_reports_only_known_states(hass, load, reported, expected):
    await load(lamp(reported))
    assert hass.states.get(entity_id(hass, "light", "lamp")).state == expected


@pytest.mark.parametrize(
    "service,action,before", [("turn_on", "on", "off"), ("turn_off", "off", "on")]
)
async def test_lamp_command_is_sent_once_and_followed_quickly(hass, load, service, action, before):
    coordinator = await load(lamp(before))
    light = entity_id(hass, "light", "lamp")
    with patch.object(
        coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
    ) as request:
        await hass.services.async_call("light", service, {"entity_id": light}, blocking=True)
    (put,) = [call for call in request.call_args_list if call.args[0] == "PUT"]
    assert put.args == ("PUT", f"{LAMPS}/api/v6.0/accounts/test-account/lamps/LAMP-1/{action}")
    assert coordinator.update_interval == timedelta(seconds=FAST_POLL_SECONDS)


async def test_lamp_already_in_the_target_state_is_not_followed(hass, load):
    coordinator = await load(lamp("off"))
    light = entity_id(hass, "light", "lamp")
    with patch.object(
        coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
    ):
        await hass.services.async_call("light", "turn_off", {"entity_id": light}, blocking=True)
    assert coordinator.update_interval == timedelta(seconds=60)


def relock(**state):
    return make_lock(
        allow_auto_relock=True, auto_relock_enabled=False, auto_relock_time=60, **state
    )


async def test_auto_relock_switch_and_delay_select(hass, load):
    coordinator = await load(relock())
    switch = entity_id(hass, "switch", "auto_relock")
    delay = entity_id(hass, "select", "auto_relock_delay")
    assert hass.states.get(switch).state == "off"
    assert hass.states.get(delay).state == "60"
    with patch.object(coordinator.management, "set_auto_relock", AsyncMock()) as write:
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
        write.assert_awaited_once_with(coordinator.data.devices[relock().key], True, 60)
        write.reset_mock()
        await hass.services.async_call(
            "select", "select_option", {"entity_id": delay, "option": "90"}, blocking=True
        )
        write.assert_awaited_once_with(coordinator.data.devices[relock().key], False, 90)
    assert coordinator.update_interval == timedelta(seconds=60)  # Settings are not moves.


async def test_auto_relock_controls_need_owner_management_rights(hass, load):
    guest = parse_account_access(access_payload(role="guest"))
    await load(relock(), access=guest)
    with pytest.raises(StopIteration):
        entity_id(hass, "switch", "auto_relock")


async def test_auto_relock_switch_refuses_direct_use_after_access_is_lost(hass, load):
    coordinator = await load(relock())
    switch = hass.data["switch"].get_entity(entity_id(hass, "switch", "auto_relock"))
    coordinator.async_set_updated_data(Snapshot(dict(coordinator.data.devices)))
    await hass.async_block_till_done()
    assert not switch.available
    with (
        patch.object(coordinator.management, "set_auto_relock", AsyncMock()) as write,
        pytest.raises(HomeAssistantError, match="not available"),
    ):
        await switch.async_turn_on()
    write.assert_not_awaited()


@pytest.mark.parametrize("option,seconds", [("off", 0), ("120", 120)])
async def test_automatic_lock_delay_select(hass, load, option, seconds):
    device = modern_lock()
    coordinator = await load(device)
    select = entity_id(hass, "select", "automatic_lock_delay")
    assert hass.states.get(select).state == "off"
    with patch.object(coordinator.management, "set_auto_lock", AsyncMock()) as write:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": select, "option": option}, blocking=True
        )
    write.assert_awaited_once_with(coordinator.data.devices[device.key], seconds)


async def test_automatic_lock_select_shows_enabled_delay(hass, load):
    enabled = make_lock(
        kind="wifilockgateway",
        automatic_lock_state="enabled",
        automatic_lock_delay_duration=30,
    )
    await load(enabled)
    assert hass.states.get(entity_id(hass, "select", "automatic_lock_delay")).state == "30"


@pytest.mark.parametrize("service,enabled", [("turn_on", True), ("turn_off", False)])
async def test_door_sensor_switch(hass, load, service, enabled):
    device = modern_lock()
    coordinator = await load(device)
    switch = entity_id(hass, "switch", "door_sensor_enabled")
    assert hass.states.get(switch).state == "on"
    with patch.object(coordinator.management, "set_door_sensor", AsyncMock()) as write:
        await hass.services.async_call("switch", service, {"entity_id": switch}, blocking=True)
    write.assert_awaited_once_with(coordinator.data.devices[device.key], enabled)


def thermostat(mode="heat", **state):
    return Device.parse(
        "a",
        {
            "serial_number": "HVAC/1",
            "name": "Hall thermostat",
            "device_type": "vendor_thermostat",
            "device_family": "hvacs",
            "state": {
                "online": True,
                "is_managed": True,
                "temperature_unit": "celsius",
                "current_temperature_celsius": 21.2,
                "current_climate_setting": {
                    "hvac_mode_setting": mode,
                    "fan_mode_setting": "auto",
                    "heating_set_point_celsius": 20.0,
                    "cooling_set_point_celsius": 25.0,
                },
                **state,
            },
            "specifications": {
                "available_hvac_mode_settings": ["off", "heat", "cool", "heat_cool"],
                "available_fan_mode_settings": ["auto", "on"],
                "min_heating_set_point_celsius": 10.0,
                "max_heating_set_point_celsius": 30.0,
                "min_cooling_set_point_celsius": 15.0,
                "max_cooling_set_point_celsius": 35.0,
            },
        },
    )


@pytest.mark.parametrize(
    "mode,state,action",
    [
        ("off", {"current_temperature_celsius": 15.0}, "off"),
        ("heat", {"current_temperature_celsius": 19.0}, "heating"),
        ("heat", {}, "idle"),
        ("cool", {"current_temperature_celsius": 26.0}, "cooling"),
        ("cool", {}, "idle"),
        ("heat_cool", {"current_temperature_celsius": 18.0}, "heating"),
        ("heat_cool", {"current_temperature_celsius": 27.0}, "cooling"),
        ("heat_cool", {}, "idle"),
        # The app never reads is_heating/is_cooling/is_fan_running.
        ("heat", {"is_heating": True, "current_temperature_celsius": 22.0}, "idle"),
        ("heat", {"current_temperature_celsius": None}, None),
    ],
)
async def test_thermostat_action_compares_current_and_target(hass, load, mode, state, action):
    await load(thermostat(mode, **state))
    climate = hass.states.async_all("climate")[0]
    assert climate.attributes.get("hvac_action") == action


@pytest.mark.parametrize(
    "mode,attribute,field,value",
    [
        ("heat", "temperature", "heating_set_point", 22),
        ("cool", "temperature", "cooling_set_point", 24),
    ],
)
async def test_single_target_follows_the_current_mode(hass, load, mode, attribute, field, value):
    coordinator = await load(thermostat(mode))
    climate = hass.states.async_all("climate")[0].entity_id
    assert hass.states.get(climate).attributes[attribute] in (20, 25)
    with patch.object(
        coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
    ) as request:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": climate, "temperature": value},
            blocking=True,
        )
    assert sent(request)[field] == value


@pytest.mark.parametrize(
    "mode,data,message",
    [
        ("heat_cool", {"temperature": 21}, "heat or cool mode"),
        ("heat", {"temperature": 21, "target_temp_low": 19}, "one temperature"),
    ],
)
async def test_ambiguous_temperature_requests_are_refused(hass, load, mode, data, message):
    coordinator = await load(thermostat(mode))
    climate = hass.states.async_all("climate")[0].entity_id
    entity = hass.data["climate"].get_entity(climate)
    with (
        patch.object(coordinator.client.http, "request", AsyncMock()) as request,
        pytest.raises(HomeAssistantError, match=message),
    ):
        await entity.async_set_temperature(**data)
    request.assert_not_awaited()


async def test_thermostat_requires_a_target(hass, load):
    await load(thermostat("heat"))
    entity = hass.data["climate"].get_entity(hass.states.async_all("climate")[0].entity_id)
    with pytest.raises(HomeAssistantError, match="required"):
        await entity.async_set_temperature()


async def test_fan_and_mode_changes_are_sent(hass, load):
    coordinator = await load(thermostat("heat"))
    climate = hass.states.async_all("climate")[0].entity_id
    with patch.object(
        coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
    ) as request:
        await hass.services.async_call(
            "climate", "set_fan_mode", {"entity_id": climate, "fan_mode": "on"}, blocking=True
        )
        assert sent(request) == {
            "action": "heat",
            "device_family": "hvacs",
            "temperature_unit": "celsius",
            "heating_set_point": 20.0,
            "fan_mode": "on",
        }
        request.reset_mock()
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": "cool"}, blocking=True
        )
        assert sent(request) == {
            "action": "cool",
            "device_family": "hvacs",
            "temperature_unit": "celsius",
            "cooling_set_point": 25.0,
            "fan_mode": "on",
        }

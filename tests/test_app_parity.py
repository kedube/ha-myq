"""Behavior matched to MyQ Android 5.324: entities from polled fields, settings and reads."""

import copy
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api import alerts as alerts_api
from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.alerts import Alerts, parse_rules, snooze_time
from custom_components.myq.api.camera_settings import CameraSettings, supported, value
from custom_components.myq.api.client import Client
from custom_components.myq.api.climate import programs
from custom_components.myq.api.const import (
    CREDENTIALS,
    DEVICES,
    GATEWAYS,
    GDO,
    LAMPS,
    PARTNER_DEVICES,
)
from custom_components.myq.api.errors import CommandRejected
from custom_components.myq.api.garage import can_set_lock_mode, is_3c_opener
from custom_components.myq.api.history import ACCOUNTHISTORY, History, parse_page
from custom_components.myq.api.http import Response
from custom_components.myq.api.management import Management, lockout_target
from custom_components.myq.api.models import Device
from custom_components.myq.api.video import VIDEO, Video
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import Snapshot
from custom_components.myq.sensor import parse_timestamp

from .conftest import json_response

OWNER = AccountAccess(
    "test-account",
    "owner",
    {key: frozenset({"All"}) for key in ("devices", "alerts", "history", "guests")},
)


@pytest.fixture
def entry(hass, enable_custom_integrations, tokens):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    return entry


async def load(hass, entry, *devices, cameras=None, access=None):
    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={device.key: device for device in devices}),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(side_effect=lambda device: copy.deepcopy((cameras or {})[device.key])),
        ),
        patch(
            "custom_components.myq.api.management.Management.account_access",
            AsyncMock(return_value=access or {}),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


def make(payload, **state):
    payload = copy.deepcopy(payload)
    payload["state"].update(state)
    return Device.parse("test-account", payload)


def lock(**state):
    return Device.parse(
        "test-account",
        {
            "serial_number": "LOCK1",
            "name": "Front",
            "device_type": "wifilockgateway",
            "device_family": "locks",
            "gateway_id": "GW-LOCK1",
            "state": {"online": True, "lock_state": "locked", **state},
        },
    )


# --- Device state: connectivity defaults and statistics ---------------------------------


def test_missing_online_is_online_but_explicit_values_decide(door_payload):
    # Device.p1: optBoolean("online", true).
    del door_payload["state"]["online"]
    assert Device.parse("a", door_payload).online
    door_payload["state"]["online"] = "true"
    assert not Device.parse("a", door_payload).online


@pytest.mark.parametrize(
    "state,online",
    [
        ({"online": False, "camera_online": True}, True),
        ({"online": True, "camera_online": False}, False),
        ({"online": True, "camera_online": None}, True),
        ({"online": False, "camera_online": None}, False),
    ],
)
def test_wifi_lock_gateway_uses_camera_connectivity_when_reported(state, online):
    # LockDevice.h2: camera_online when non-null, otherwise online.
    assert lock(**state).online is online


async def test_statistics_classes_and_enum_values(hass, entry, door_payload):
    door = make(
        door_payload,
        battery_backup_state="Charging",
        absolute_cycle_count="1204",
        last_update="2026-11-20T08:15:30.123Z",
    )
    await load(hass, entry, door)
    cycles = hass.states.get("sensor.test_garage_door_cycles")
    assert cycles.state == "1204"
    assert cycles.attributes["state_class"] == "total_increasing"
    backup = hass.states.get("sensor.test_garage_backup_battery")
    assert backup.state == "charging"
    assert backup.attributes["options"] == [
        "idle",
        "charging",
        "bad",
        "disconnected",
        "none",
        "unknown",
    ]
    changed = hass.states.get("sensor.test_garage_last_state_change")
    assert changed.state == "2026-11-20T08:15:30+00:00"
    assert hass.states.get("binary_sensor.test_garage_backup_battery_problem").state == "off"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-11-20T08:15:30", datetime(2026, 11, 20, 8, 15, 30, tzinfo=UTC)),
        ("2026-11-20T08:15:30+05:00", datetime(2026, 11, 20, 8, 15, 30, tzinfo=UTC)),
        ("2026-11-20", None),
        ("", None),
        (None, None),
        ("not-a-timestamp-value", None),
    ],
)
def test_last_update_parses_like_simple_date_format(value, expected):
    # ct0/a parses the leading yyyy-MM-dd'T'HH:mm:ss as UTC and ignores the rest.
    assert parse_timestamp(value) == expected


async def test_offline_device_keeps_battery_and_offline_reason(hass, entry):
    device = lock(
        online=False,
        battery_percentage="40",
        low_battery=True,
        offline_reason="gateway offline",
        last_status="2026-11-20T07:00:00",
        last_update="2026-11-20T09:00:00",
    )
    await load(hass, entry, device)
    assert hass.states.get("sensor.front_battery").state == "40"
    assert hass.states.get("sensor.front_battery").attributes["state_class"] == "measurement"
    assert hass.states.get("binary_sensor.front_low_battery").state == "on"
    assert hass.states.get("sensor.front_offline_reason").state == "gateway offline"
    # Device.D0 shows last_status while offline.
    assert hass.states.get("sensor.front_last_state_change").state.startswith("2026-11-20T07:00:00")
    assert hass.states.get("lock.front").state == "unavailable"


async def test_opener_problem_sensors(hass, entry, door_payload):
    door = make(
        door_payload,
        sensor_comm_error=True,
        number_of_learned_dps_devices=0,
        attached_work_light_error_present=True,
        is_unattended_close_allowed=False,
        in_vacation_mode=True,
        active_fault_codes=[
            {"code": "E12", "description": "Sensor", "activated_timestamp": "2026-11-20T08:00"},
            "junk",
        ],
    )
    await load(hass, entry, door)
    # GdoDeviceUiItem: no learned sensor, so no communication fault.
    assert hass.states.get("binary_sensor.test_garage_door_sensor_communication_fault").state == (
        "off"
    )
    assert hass.states.get("binary_sensor.test_garage_work_light_not_paired").state == "on"
    assert hass.states.get("binary_sensor.test_garage_safety_sensor_alignment").state == "on"
    assert hass.states.get("binary_sensor.test_garage_remotes_disabled").state == "on"
    fault = hass.states.get("binary_sensor.test_garage_fault")
    assert fault.state == "on"
    # Malformed entries are dropped.
    assert fault.attributes["faults"] == [
        {"code": "E12", "description": "Sensor", "activated_timestamp": "2026-11-20T08:00"},
    ]
    # The app refuses control while the work light reports an error.
    assert hass.states.get("cover.test_garage").attributes["supported_features"] == 0


async def test_lock_sensors_from_polled_fields(hass, entry):
    device = lock(
        low_battery=False,
        failed_credential_lockout_active=True,
        failed_credential_lockout_enabled=True,
        failed_credential_lockout_frequency=5,
        orientation="CalibrationFailure",
        door_sensor_state="Enabled",
        door_state="open",
        firmware_version="1.2.0",
        firmware_available=True,
        latest_available_firmware_version="1.3.0",
        lock_model="Smart Lock X",
    )
    await load(hass, entry, device)
    assert hass.states.get("binary_sensor.front_low_battery").state == "off"
    assert hass.states.get("binary_sensor.front_pin_lockout").state == "on"
    assert hass.states.get("binary_sensor.front_lock_calibration").state == "on"
    assert hass.states.get("sensor.front_lock_orientation").state == "calibrationfailure"
    assert hass.states.get("binary_sensor.front_door").state == "on"
    firmware = hass.states.get("update.front_firmware")
    assert firmware.state == "on"
    assert firmware.attributes["installed_version"] == "1.2.0"
    assert firmware.attributes["latest_version"] == "1.3.0"
    registry = er.async_get(hass)
    disabled = registry.async_get("sensor.front_pin_lockout_attempts")
    assert disabled is not None and disabled.disabled_by is not None
    # PIN lockout blocks lock commands, as the app's precheck does.
    assert not hass.states.get("lock.front").attributes.get("supported_features")


async def test_camera_status_sensors_firmware_and_mac(hass, entry, camera_device, camera_details):
    camera_details["status"].update(
        upgradeInProgress=False,
        pwr={"battLv": 64, "pwrSrc": "SOLAR+BATT", "battChrgMw": 120, "sleepMode": "FULL_POWER"},
    )
    camera_details["net"] = {"eth": {"wifiStrength": 72, "mac": "AA-BB-CC-00-11-22"}}
    camera_details["hw"] = {"firmwareVer": "2.0.1", "firmwareUpgradeAvail": "2.1.0"}
    await load(hass, entry, camera_device, cameras={camera_device.key: camera_details})
    assert hass.states.get("sensor.test_camera_battery").state == "64"
    assert hass.states.get("sensor.test_camera_power_source").state == "solar_battery"
    assert hass.states.get("sensor.test_camera_wi_fi_strength").state == "72"
    assert hass.states.get("binary_sensor.test_camera_charging").state == "on"
    firmware = hass.states.get("update.test_camera_firmware")
    assert firmware.attributes["installed_version"] == "2.0.1"
    assert firmware.attributes["latest_version"] == "2.1.0"
    device = next(iter(hass.data["device_registry"].devices.values()))
    assert device.sw_version == "2.0.1"
    assert ("mac", "aa:bb:cc:00:11:22") in device.connections


# --- Camera settings ---------------------------------------------------------------------


def settings_details(**extra):
    details = {
        "status": {"online": True},
        "cap": {"cam": {}, "chime": {"typeList": ["MECHANICAL", "DIGITAL", "NONE"]}},
        "settings": {"cam": {}, "chime": {"enabled": False, "type": "DIGITAL", "duration": 8}},
    }
    for path, item in extra.items():
        group, key = path.split("__")
        details[group][key] = item
    return details


@pytest.mark.parametrize(
    "key,desired,details,payload",
    [
        (
            "wake_on_motion",
            False,
            settings_details(cap__wakeOnMotionCap=True, settings__wakeOnMotion=True),
            {"wakeOnMotion": False},
        ),
        (
            "power_saver",
            "on",
            settings_details(
                cap__pwr={"powerSaverModes": ["AUTO", "ON", "OFF"]},
                settings__pwr={"powerSaverMode": "AUTO"},
            ),
            {"pwr": {"powerSaverMode": "ON"}},
        ),
        (
            "chime_type",
            "mechanical",
            settings_details(),
            # hb0/m: mechanical carries duration 0; the enabled state is kept.
            {"chime": {"enabled": False, "type": "MECHANICAL", "duration": 0}},
        ),
    ],
)
async def test_new_camera_setting_payloads(
    http, tokens, camera_device, key, desired, details, payload
):
    http.request.side_effect = [json_response(details), Response(204, {}, b"")]
    await CameraSettings(Video(Client(transport=http, tokens=tokens))).set(
        camera_device, key, desired
    )
    assert http.request.call_args.kwargs["json"] == payload


def test_wake_on_motion_defers_to_power_saver_modes():
    details = settings_details(
        cap__wakeOnMotionCap=True,
        settings__wakeOnMotion=True,
        cap__pwr={"powerSaverModes": ["AUTO", "OFF"]},
        settings__pwr={"powerSaverMode": "OFF"},
    )
    assert not supported(details, "wake_on_motion")
    assert supported(details, "power_saver") and value(details, "power_saver") == "off"


@pytest.mark.parametrize(
    "siren,vol,ok",
    [
        ({"activatable": True}, 80, True),
        ({"activatable": False, "minVol": 0, "maxVol": 100}, 80, False),
        ({"minVol": 0, "maxVol": 100}, 80, False),
    ],
)
def test_siren_volume_requires_an_activatable_siren(siren, vol, ok):
    details = settings_details(cap__siren=siren, settings__siren={"vol": vol})
    assert supported(details, "siren_volume") is ok


def test_status_led_is_read_without_regard_to_case():
    details = settings_details(
        cap__statusLed={"ledOnOff": True}, settings__statusLed={"status": "on"}
    )
    assert value(details, "status_led") is True


async def test_settings_write_wakes_a_sleeping_camera_first(
    http, tokens, camera_device, monkeypatch
):
    monkeypatch.setattr("custom_components.myq.api.video.WAKEUP_POLL_INTERVAL", 0)
    asleep = settings_details(cap__wakeOnMotionCap=True, settings__wakeOnMotion=True)
    asleep["status"]["pwr"] = {"sleepMode": "LOW_POWER_DEEP_SLEEP"}
    awake = copy.deepcopy(asleep)
    awake["status"]["pwr"]["sleepMode"] = "FULL_POWER"
    http.request.side_effect = [
        json_response(asleep),
        Response(204, {}, b""),
        json_response(awake),
        Response(204, {}, b""),
    ]
    await CameraSettings(Video(Client(transport=http, tokens=tokens))).set(
        camera_device, "wake_on_motion", False
    )
    calls = [call.args[:2] for call in http.request.call_args_list]
    assert calls[1] == ("POST", VIDEO + "/cxs/api/devices/CAM123/do/wakeup")
    assert calls[-1] == ("PUT", VIDEO + "/cxs/api/devices/CAM123/settings")


# --- Management: lock mode, PIN lockout, rename -------------------------------------------


def management(tokens, http, *devices):
    client = Client(transport=http, tokens=tokens)
    client.devices = AsyncMock(return_value=list(devices))
    manager = Management(client)
    manager.poll_interval = 0
    manager.account_access = AsyncMock(return_value={"test-account": OWNER})
    return manager


def test_lock_mode_is_for_owners_of_wifi_or_3c_openers(door):
    assert is_3c_opener(replace(door, serial="GW3C00012345"))
    assert not is_3c_opener(replace(door, serial="3C01"))  # Too short, as in the app.
    gdo = replace(door, device_type="garagedooropener", serial="39000012345")
    assert not can_set_lock_mode(gdo, OWNER)
    assert can_set_lock_mode(replace(gdo, serial="3C000012345"), OWNER)
    assert can_set_lock_mode(door, OWNER)  # Wi-Fi opener.
    co_owner = AccountAccess("test-account", "co_owner", {"devices": frozenset({"All"})})
    assert not can_set_lock_mode(door, co_owner)


async def test_lock_mode_wire_and_readback(tokens, http, door):
    enabled = replace(door, state={**door.state, "in_vacation_mode": True})
    manager = management(tokens, http, door)
    manager.client.devices = AsyncMock(side_effect=[[door], [enabled]])
    http.request.return_value = Response(204, {}, b"")
    await manager.set_lock_mode(door, True)
    http.request.assert_awaited_once()
    assert http.request.call_args.args == (
        "PUT",
        f"{GDO}/api/v6.0/accounts/test-account/door_openers/TEST-GDO-1/lockmode",
    )
    assert http.request.call_args.kwargs["json"] == {"enable_lock_mode": True}


async def test_pin_lockout_reset_targets_the_gateway(tokens, http):
    active = lock(failed_credential_lockout_active=True)
    cleared = lock(failed_credential_lockout_active=False)
    manager = management(tokens, http, active)
    manager.client.devices = AsyncMock(side_effect=[[active], [cleared]])
    http.request.return_value = Response(204, {}, b"")
    await manager.reset_pin_lockout(active)
    assert http.request.call_args.args == (
        "POST",
        f"{CREDENTIALS}/api/v1.0/accounts/test-account/devices/GW-LOCK1/lockout/reset",
    )
    assert http.request.call_args.kwargs["json"] == {"credential_type": "pin"}


def test_lockout_target_order():
    # Device.s(): gateway, then parent_serial_number, then the device itself.
    no_gateway = replace(lock(parent_serial_number="PARENT"), gateway_id=None)
    assert lockout_target(no_gateway) == "PARENT"
    assert lockout_target(replace(lock(), gateway_id=None)) == "LOCK1"


async def test_pin_lockout_reset_requires_an_active_lockout(tokens, http):
    manager = management(tokens, http, lock(failed_credential_lockout_active=False))
    with pytest.raises(CommandRejected):
        await manager.reset_pin_lockout(lock())
    http.request.assert_not_awaited()


@pytest.mark.parametrize(
    "device_type,family,base,path",
    [
        ("lamp", "lamp", LAMPS, "/api/v6.0/accounts/test-account/lamps/DEV1"),
        ("gate", "garagedoor", GDO, "/api/v6.0/accounts/test-account/door_openers/DEV1"),
        ("hub", "gateway", GATEWAYS, "/api/v6.0/accounts/test-account/gateways/DEV1"),
        ("wifilockgateway", "gateway", DEVICES, "/api/v6.0/Accounts/test-account/Devices/DEV1"),
        ("hawkeye-camera", "camera", DEVICES, "/api/v6.0/Accounts/test-account/Devices/DEV1"),
    ],
)
async def test_rename_routes_by_family(tokens, http, device_type, family, base, path):
    device = Device.parse(
        "test-account",
        {
            "serial_number": "DEV1",
            "name": "Old",
            "device_type": device_type,
            "device_family": family,
            "state": {},
        },
    )
    renamed = replace(device, name="New name")
    manager = management(tokens, http, device)
    manager.client.devices = AsyncMock(side_effect=[[device], [renamed]])
    http.request.return_value = Response(204, {}, b"")
    await manager.rename_device(device, "New name")
    assert http.request.call_args.args == ("PUT", base + path)
    assert http.request.call_args.kwargs["json"] == {"name": "New name"}


@pytest.mark.parametrize("name", ["", "   ", "x" * 25, "bad\nname"])
async def test_rename_rejects_names_the_app_would_not_accept(tokens, http, door, name):
    with pytest.raises(CommandRejected):
        await management(tokens, http, door).rename_device(door, name)
    http.request.assert_not_awaited()


async def test_lock_mode_switch_and_clear_lockout_button(hass, entry, door):
    gated = replace(door, state={**door.state, "in_vacation_mode": None})
    pinned = lock(failed_credential_lockout_active=True)
    await load(hass, entry, gated, pinned, access={"test-account": OWNER})
    assert hass.states.get("switch.test_garage_disable_remotes").state == "off"
    # The diagnostic copy starts disabled once the switch shows it.
    registry = er.async_get(hass)
    copy_entry = registry.async_get("binary_sensor.test_garage_remotes_disabled")
    assert copy_entry is None or copy_entry.disabled_by is not None
    button = registry.async_get("button.front_clear_pin_lockout")
    assert button is not None and button.disabled_by is not None


# --- History, alerts and thermostat programs -----------------------------------------------


def history_payload():
    return {
        "continuation_token": "next-page",
        "device_history": [
            {
                "id": "h2",
                "event_timestamp": "2026-11-20T08:00:05:123Z",
                "event_type": "DEVICE_ACTION",
                "event_by": {"name": "Sam", "user_id": "secret-user", "source": "app"},
                "event": {"device_serial_number": "TEST-GDO-1", "device_event": "open"},
            },
            {
                "id": "h1",
                "event_timestamp": "2026-11-20T07:59:00Z",
                "event_type": "key_press",
                "event": {"device_serial_number": "TEST-GDO-1", "device_event": "mystery"},
            },
            {"id": "bad", "event_timestamp": "yesterday"},
        ],
    }


def test_history_page_is_ordered_and_omits_user_ids():
    page = parse_page(history_payload())
    assert [event.id for event in page.events] == ["h1", "h2"]
    assert page.events[0].device_event == "other"
    assert page.events[1].timestamp == datetime(2026, 11, 20, 8, 0, 5, 123000, tzinfo=UTC)
    assert page.continuation == "next-page"
    assert "secret-user" not in str([event.as_dict() for event in page.events])


async def test_history_read_wire(http, tokens):
    http.request.return_value = json_response(history_payload())
    await History(Client(transport=http, tokens=tokens)).read("acct/1", ["B", "A"], "tok")
    assert http.request.call_args.args == (
        "GET",
        f"{ACCOUNTHISTORY}/api/v6.1/accounts/acct%2F1/deviceHistory",
    )
    assert http.request.call_args.kwargs["params"] == {"devices": "A,B", "continue": "tok"}


async def test_history_event_entity_fires_only_new_entries(hass, entry, door):
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "event", DOMAIN, f"{door.key}:history", config_entry=entry, disabled_by=None
    )
    coordinator = await load(hass, entry, door, access={"test-account": OWNER})
    assert door.key in coordinator._history_devices
    events = parse_page(history_payload()).events
    coordinator.async_set_updated_data(Snapshot({door.key: door}, history={door.key: events[:1]}))
    await hass.async_block_till_done()
    entity_id = registry.async_get_entity_id("event", DOMAIN, f"{door.key}:history")
    assert hass.states.get(entity_id).state == "unknown"  # Priming read fires nothing.
    coordinator.async_set_updated_data(Snapshot({door.key: door}, history={door.key: events}))
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["event_type"] == "open"
    assert state.attributes["activity"] == "device_action"
    assert state.attributes["by"] == "Sam"


def alert(**changes):
    return {
        "id": "rule-1",
        "serial_number": "TEST-GDO-1",
        "display_name": "Left open",
        "is_enabled": True,
        "linked_alert_id": None,
        "details": [{"event_type": "open", "notification_preferences": ["delayed"]}],
        "schedules": [{"day": "monday", "start_time": "00:00", "end_time": "23:59"}],
        "is_auto_created": False,
        **changes,
    }


async def test_alert_toggle_sends_the_full_dto(http, tokens, door):
    rule = parse_rules({"items": [alert(unexpected="kept out")]})[0]
    http.request.return_value = Response(204, {}, b"")
    await Alerts(Client(transport=http, tokens=tokens)).set_enabled(door, rule, False)
    assert http.request.call_args.args == (
        "PUT",
        "https://alerts.myq-cloud.com/api/v6.1/accounts/test-account/alerts/rule-1",
    )
    assert http.request.call_args.kwargs["json"] == {**alert(), "is_enabled": False}


def test_snooze_time_format():
    # ho0/f: yyyy-MM-dd'T'HH:mm:ss.SSS'Z' in UTC.
    assert snooze_time(datetime(2026, 11, 20, 8, 0, 5, 7000, tzinfo=UTC)) == (
        "2026-11-20T08:00:05.007Z"
    )


async def test_snooze_wire(http, tokens, door):
    http.request.return_value = Response(204, {}, b"")
    body = await Alerts(Client(transport=http, tokens=tokens)).snooze(door, 60)
    assert http.request.call_args.args == (
        "POST",
        f"{alerts_api.ALERTS}/api/v6.0/accounts/test-account/alerts/snooze",
    )
    assert http.request.call_args.kwargs["json"] == body
    with pytest.raises(CommandRejected):
        await Alerts(Client(transport=http, tokens=tokens)).snooze(door, 0)


async def test_alert_rule_switch_toggles_and_confirms(hass, entry, door):
    coordinator = await load(hass, entry, door)
    coordinator.async_set_updated_data(
        Snapshot(
            {door.key: door},
            account_access={"test-account": OWNER},
            alerts={door.key: parse_rules({"items": [alert()]})},
        )
    )
    await hass.async_block_till_done()
    switch = hass.states.get("switch.test_garage_alert_left_open")
    assert switch.state == "on"
    with (
        patch.object(
            coordinator.management,
            "account_access",
            AsyncMock(return_value={"test-account": OWNER}),
        ),
        patch.object(
            coordinator.alerts,
            "rules",
            AsyncMock(
                side_effect=[
                    parse_rules({"items": [alert()]}),
                    parse_rules({"items": [alert(is_enabled=False)]}),
                ]
            ),
        ),
        patch.object(coordinator.alerts, "set_enabled", AsyncMock()) as write,
        patch.object(coordinator.client, "discover", AsyncMock(return_value={door.key: door})),
    ):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch.entity_id}, blocking=True
        )
    write.assert_awaited_once()
    assert write.call_args.args[2] is False


async def test_thermostat_programs_are_read_only_gets(http, tokens):
    thermostat = Device.parse(
        "test-account",
        {
            "serial_number": "HVAC1",
            "device_type": "x_thermostat",
            "device_family": "hvacs",
            "state": {"online": True},
        },
    )
    http.request.side_effect = [
        json_response(
            {
                "presets": [
                    {
                        "climate_preset_key": "p1",
                        "name": "Home",
                        "heating_set_point_celsius": 20.5,
                        "extra": "dropped",
                    }
                ]
            }
        ),
        json_response(
            {
                "schedules": [
                    {
                        "climate_schedule_key": "s1",
                        "days": [1, 7, 9],
                        "scheduled_presets": [{"start_time": "07:00", "climate_preset_key": "p1"}],
                    }
                ]
            }
        ),
    ]
    result = await programs(Client(transport=http, tokens=tokens), thermostat)
    paths = [call.args for call in http.request.call_args_list]
    base = f"{PARTNER_DEVICES}/api/v1.0/accounts/test-account/devices/thermostats/HVAC1"
    assert paths == [("GET", base + "/presets"), ("GET", base + "/schedules")]
    assert result["presets"] == [
        {"climate_preset_key": "p1", "name": "Home", "heating_set_point_celsius": 20.5}
    ]
    assert result["schedules"][0]["days"] == [1, 7]


async def test_lamp_honors_monitor_only(http, tokens):
    lamp = Device.parse(
        "test-account",
        {
            "serial_number": "L1",
            "device_type": "lamp",
            "device_family": "lamp",
            "state": {"online": True, "monitor_only_mode": True},
        },
    )
    with pytest.raises(CommandRejected, match="monitor-only"):
        await Client(transport=http, tokens=tokens).lamp(lamp, True)
    http.request.assert_not_awaited()

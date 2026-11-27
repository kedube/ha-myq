"""Real HA service boundaries, private PIN handoff and registry scoping."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import device_registry as dr

from custom_components.myq.api.associations import associations
from custom_components.myq.api.errors import CommandRejected
from custom_components.myq.api.management import Accessory
from custom_components.myq.const import DOMAIN
from custom_components.myq.services import SCHEMAS, _read_pin

from .ha import MockConfigEntry, registry_device


@pytest.fixture
async def integration(hass, enable_custom_integrations, tokens, door):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user", data={"tokens": tokens.as_dict()})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.myq.api.client.Client.discover",
        AsyncMock(return_value={door.key: door}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        device_id = registry_device(dr.async_get(hass), entry.entry_id, (DOMAIN, door.key)).id
        yield entry, device_id
        if entry.state.value == "loaded":
            await hass.config_entries.async_unload(entry.entry_id)


async def test_admin_service_targets_current_device(hass, integration, door, hass_admin_user):
    entry, device_id = integration
    method = AsyncMock()
    with patch.object(entry.runtime_data.management, "rename_accessory", method):
        await hass.services.async_call(
            DOMAIN,
            "rename_accessory",
            {"device_id": device_id, "accessory_serial": "remote", "name": "Back door"},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
    method.assert_awaited_once_with(door, accessory_serial="remote", name="Back door")


async def test_non_admin_cannot_modify_guests(hass, integration, hass_read_only_user):
    entry, device_id = integration
    with (
        patch.object(entry.runtime_data.management, "remove_guest", AsyncMock()) as remove,
        pytest.raises(Unauthorized),
    ):
        await hass.services.async_call(
            DOMAIN,
            "remove_guest",
            {"device_id": device_id, "account_user_id": "guest"},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    remove.assert_not_awaited()


async def test_removed_device_and_extra_endpoint_fields_are_rejected(hass, integration):
    entry, device_id = integration
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "remove_accessory",
            {
                "device_id": device_id,
                "accessory_serial": "remote",
                "url": "https://elsewhere.invalid",
            },
            blocking=True,
        )
    entry.runtime_data.data.devices.clear()
    with pytest.raises(HomeAssistantError, match="registered"):
        await hass.services.async_call(
            DOMAIN,
            "remove_accessory",
            {"device_id": device_id, "accessory_serial": "remote"},
            blocking=True,
        )


async def test_pin_value_never_enters_ha_service_event(hass, integration, door, tmp_path, caplog):
    entry, device_id = integration
    hass.config.config_dir = str(tmp_path)
    pin_file = tmp_path / "myq-pin.json"
    pin_file.write_text('{"pin":"479135"}')
    pin_file.chmod(0o600)
    events = []
    unsubscribe = hass.bus.async_listen("call_service", lambda event: events.append(event.data))
    try:
        with patch.object(entry.runtime_data.management, "save_pin", AsyncMock()) as save:
            await hass.services.async_call(
                DOMAIN,
                "save_pin",
                {"device_id": device_id, "name": "Visitor", "pin_file": pin_file.name},
                blocking=True,
            )
        save.assert_awaited_once_with(door, pin="479135", name="Visitor")
        await hass.async_block_till_done()
        assert events and "479135" not in json.dumps(events)
        assert "479135" not in caplog.text
        assert "479135" not in repr(entry.data)
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                "save_pin",
                {"device_id": device_id, "name": "Visitor", "pin": "479135"},
                blocking=True,
            )
    finally:
        unsubscribe()


@pytest.mark.parametrize("kind", ["public", "symlink", "traversal", "invalid", "large"])
def test_pin_file_rejects_unsafe_inputs(tmp_path, kind):
    path = tmp_path / "pin.json"
    path.write_text('{"pin":"479135"}')
    path.chmod(0o600)
    filename = path.name
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        filename = link.name
    elif kind == "traversal":
        filename = "../pin.json"
    elif kind == "invalid":
        path.write_text('{"pin":479135}')
    else:
        path.write_text('{"pin":"' + "1" * 5000 + '"}')
    with pytest.raises(CommandRejected) as error:
        _read_pin(str(tmp_path), filename)
    assert "479135" not in str(error.value)


async def test_accessory_response_enables_battery_inventory(hass, integration, door):
    entry, device_id = integration
    accessory = Accessory("remote", "Keypad", "S3Keypad", False, "good")
    with (
        patch.object(entry.runtime_data.management, "require_access", AsyncMock()),
        patch.object(
            entry.runtime_data.management, "accessories", AsyncMock(return_value=(accessory,))
        ),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "get_accessories",
            {"device_id": device_id},
            blocking=True,
            return_response=True,
        )
    assert result["accessories"][0]["battery"] == "good"
    assert door.key in entry.runtime_data._tracked_accessories


async def test_services_outlive_entries_but_refuse_calls_without_one(hass, integration):
    """Actions are registered in async_setup; unloading the last entry disables them."""
    entry, device_id = integration
    coordinator = entry.runtime_data
    assert all(hass.services.has_service(DOMAIN, name) for name in SCHEMAS)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert all(hass.services.has_service(DOMAIN, name) for name in SCHEMAS)
    coordinator.status_monitor.close.assert_awaited_once()
    with pytest.raises(HomeAssistantError, match="registered MyQ device"):
        await hass.services.async_call(
            DOMAIN, "get_pins", {"device_id": device_id}, blocking=True, return_response=True
        )
    with pytest.raises(HomeAssistantError, match="not loaded"):
        await hass.services.async_call(
            DOMAIN,
            "validate_session",
            {"config_entry_id": entry.entry_id},
            blocking=True,
            return_response=True,
        )


async def test_first_schedule_service_uses_bounded_creation_path(hass, integration, door):
    entry, device_id = integration
    values = {
        "feature": "floodlight",
        "label": "Evening",
        "days": ["monday"],
        "start_time": "18:00:00",
        "end_time": "22:00:00",
        "enabled": True,
    }
    with patch.object(entry.runtime_data.management, "create_schedule", AsyncMock()) as create:
        await hass.services.async_call(
            DOMAIN, "save_schedule", {"device_id": device_id, **values}, blocking=True
        )
    create.assert_awaited_once_with(door, **values)
    with pytest.raises(HomeAssistantError, match="configuration"):
        await hass.services.async_call(
            DOMAIN,
            "save_schedule",
            {"device_id": device_id, "schedule_id": "existing", **values},
            blocking=True,
        )


async def test_card_static_asset_is_served(hass, integration, hass_client):
    client = await hass_client()
    response = await client.get("/myq/live-card.js")
    assert response.status == 200
    source = await response.text()
    assert 'customElements.define("myq-live-card"' in source
    assert "test-access" not in source


def test_associations_resolve_only_observed_same_account_records(door, camera_device):
    camera = replace(
        camera_device, state={"associated_gdo_serial_numbers": [door.serial, door.serial]}
    )
    linked_door = replace(door, state={"associated_camera_serial_number": camera.serial})
    inventory = {linked_door.key: linked_door, camera.key: camera}
    assert associations(linked_door, inventory)["cameras"] == (camera,)
    assert associations(camera, inventory)["garage_doors"] == (linked_door,)
    wrong_account = replace(camera, account_id="different-account")
    assert associations(linked_door, {wrong_account.key: wrong_account})["cameras"] == ()
    malformed = replace(camera, state={"associated_gdo_serial_numbers": [door.serial, 42]})
    assert associations(malformed, inventory)["garage_doors"] == ()


async def test_validate_session_routes_to_the_selected_entry(hass, integration):
    entry, _ = integration
    with patch(
        "custom_components.myq.services.validate_session",
        AsyncMock(return_value={"accounts": 1, "devices": 1}),
    ) as validate:
        response = await hass.services.async_call(
            DOMAIN,
            "validate_session",
            {"config_entry_id": entry.entry_id, "refresh": True},
            blocking=True,
            return_response=True,
        )
    assert response == {"accounts": 1, "devices": 1}
    validate.assert_awaited_once_with(
        entry.runtime_data.client, expected_user_id="user", refresh=True
    )


async def test_read_only_services_return_serialized_inventories(hass, integration, door):
    from custom_components.myq.api.management import (
        Guest,
        PinMetadata,
        WorkflowSchedule,
    )

    entry, device_id = integration
    management = entry.runtime_data.management
    schedule = WorkflowSchedule("config-1", "s1", "floodlight", "Evening", True, "monday", "1", "2")
    with (
        patch.object(management, "require_access", AsyncMock()) as require,
        patch.object(management, "schedules", AsyncMock(return_value=(schedule,))),
        patch.object(management, "configuration_ids", return_value=["config-1"]),
        patch.object(
            management,
            "pins",
            AsyncMock(return_value=(PinMetadata("p", "Door", "enabled", "synced"),)),
        ),
        patch.object(
            management, "guests", AsyncMock(return_value=(Guest("g", "Guest", "guest", ()),))
        ),
    ):
        schedules = await hass.services.async_call(
            DOMAIN,
            "get_schedules",
            {"device_id": device_id, "feature": "floodlight"},
            blocking=True,
            return_response=True,
        )
        pins = await hass.services.async_call(
            DOMAIN, "get_pins", {"device_id": device_id}, blocking=True, return_response=True
        )
        guests = await hass.services.async_call(
            DOMAIN, "get_guests", {"device_id": device_id}, blocking=True, return_response=True
        )
    require.assert_awaited_once_with(door.account_id, "schedules", "View")
    assert schedules["configuration_ids"] == ["config-1"]
    assert schedules["schedules"][0]["label"] == "Evening"
    assert pins == {"pins": [{"id": "p", "name": "Door", "access": "enabled", "status": "synced"}]}
    assert guests["guests"][0]["role"] == "guest"


async def test_delay_dropdown_values_are_accepted_and_others_refused(hass, integration, door):
    entry, device_id = integration
    with patch.object(entry.runtime_data.management, "set_auto_lock", AsyncMock()) as write:
        await hass.services.async_call(
            DOMAIN, "set_auto_lock", {"device_id": device_id, "seconds": "60"}, blocking=True
        )
        write.assert_awaited_once_with(door, seconds=60)
        for value in ("45", 30.5, True):
            with pytest.raises(vol.Invalid):
                await hass.services.async_call(
                    DOMAIN,
                    "set_auto_lock",
                    {"device_id": device_id, "seconds": value},
                    blocking=True,
                )
    write.assert_awaited_once()


@pytest.mark.parametrize("service", ["remove_accessory", "rename_accessory"])
async def test_accessory_changes_drop_the_cached_inventory(hass, integration, door, service):
    entry, device_id = integration
    coordinator = entry.runtime_data
    coordinator._accessory_cache[door.key] = (0.0, ())
    data = {"device_id": device_id, "accessory_serial": "TX02-test"}
    if service == "rename_accessory":
        data["name"] = "Back door"
    with patch.object(coordinator.management, service, AsyncMock()):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    assert door.key not in coordinator._accessory_cache

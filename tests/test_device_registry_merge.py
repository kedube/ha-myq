"""Registry migration retains entities when an embedded gateway joins its opener."""

from dataclasses import replace

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.const import DOMAIN
from custom_components.myq.device_registry import async_merge_opener_gateway


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def gateway(door):
    return replace(
        door,
        serial="TEST-GATEWAY",
        name="Wi-Fi gateway",
        device_type="wifigdogateway",
        family="gateway",
        model=None,
        state={"firmware_version": "synthetic-firmware"},
    )


def registered(hass, entry, device, **kwargs):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.key)},
        name=device.name,
        **kwargs,
    )


@pytest.mark.parametrize("existing", ["neither", "opener", "gateway"])
async def test_new_pair_or_single_existing_row_retains_its_id(hass, entry, door, gateway, existing):
    original = None
    if existing != "neither":
        original = registered(hass, entry, door if existing == "opener" else gateway)
    assert async_merge_opener_gateway(hass, entry, door, gateway)
    devices = dr.async_get(hass)
    opener_row = devices.async_get_device(identifiers={(DOMAIN, door.key)})
    gateway_row = devices.async_get_device(identifiers={(DOMAIN, gateway.key)})
    assert opener_row.id == gateway_row.id
    if original is not None:
        assert opener_row.id == original.id
    assert opener_row.name == door.name
    assert opener_row.model == door.model
    assert opener_row.sw_version == "synthetic-firmware"
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 1
    assert async_merge_opener_gateway(hass, entry, door, gateway)
    assert devices.async_get_device(identifiers={(DOMAIN, door.key)}).id == opener_row.id


async def test_existing_rows_move_entities_keep_preferences_and_reparent_camera(
    hass, entry, door, gateway
):
    devices, entities = dr.async_get(hass), er.async_get(hass)
    garage = ar.async_get(hass).async_create("Garage")
    other_area = ar.async_get(hass).async_create("Utility")
    opener_row = registered(hass, entry, door)
    gateway_row = registered(
        hass, entry, gateway, connections={(dr.CONNECTION_NETWORK_MAC, "02:00:00:00:00:01")}
    )
    devices.async_update_device(
        opener_row.id, area_id=garage.id, name_by_user="My garage", labels={"opener-label"}
    )
    devices.async_update_device(
        gateway_row.id, area_id=other_area.id, name_by_user="My gateway", labels={"wifi-label"}
    )
    devices.async_update_device(opener_row.id, via_device_id=gateway_row.id)
    camera = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "synthetic-camera")},
        name="Camera",
        via_device=(DOMAIN, gateway.key),
    )
    original_entities = []
    for suffix, disabled in (("signal", None), ("model", er.RegistryEntryDisabler.USER)):
        entity = entities.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{gateway.key}:{suffix}",
            config_entry=entry,
            device_id=gateway_row.id,
            disabled_by=disabled,
        )
        entity = entities.async_update_entity(
            entity.entity_id, name="Custom " + suffix, area_id=other_area.id, icon="mdi:wifi"
        )
        original_entities.append(entity)

    assert async_merge_opener_gateway(hass, entry, door, gateway)
    await hass.async_block_till_done()
    assert devices.async_get(gateway_row.id) is None
    combined = devices.async_get(opener_row.id)
    assert combined.identifiers == {(DOMAIN, door.key), (DOMAIN, gateway.key)}
    assert combined.area_id == garage.id
    assert combined.name_by_user == "My garage"
    assert combined.labels == {"opener-label", "wifi-label"}
    assert combined.connections == gateway_row.connections
    assert combined.via_device_id is None
    assert devices.async_get(camera.id).via_device_id == combined.id
    for original in original_entities:
        migrated = entities.async_get(original.entity_id)
        assert migrated.device_id == combined.id
        assert migrated.unique_id == original.unique_id
        assert migrated.name == original.name
        assert migrated.area_id == original.area_id
        assert migrated.icon == original.icon
        assert migrated.disabled_by == original.disabled_by


async def test_gateway_preferences_fill_empty_opener_preferences(hass, entry, door, gateway):
    devices = dr.async_get(hass)
    area = ar.async_get(hass).async_create("Garage")
    opener_row = registered(hass, entry, door)
    gateway_row = registered(hass, entry, gateway)
    devices.async_update_device(gateway_row.id, area_id=area.id, name_by_user="Original label")
    assert async_merge_opener_gateway(hass, entry, door, gateway)
    combined = devices.async_get(opener_row.id)
    assert combined.area_id == area.id
    assert combined.name_by_user == "Original label"


async def test_gateway_device_disabled_entities_remain_disabled(hass, entry, door, gateway):
    entities = er.async_get(hass)
    opener_row = registered(hass, entry, door)
    gateway_row = registered(hass, entry, gateway, disabled_by=dr.DeviceEntryDisabler.USER)
    sensor = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{gateway.key}:signal",
        config_entry=entry,
        device_id=gateway_row.id,
        disabled_by=er.RegistryEntryDisabler.DEVICE,
    )
    assert async_merge_opener_gateway(hass, entry, door, gateway)
    await hass.async_block_till_done()
    moved = entities.async_get(sensor.entity_id)
    assert moved.device_id == opener_row.id
    assert moved.disabled_by is er.RegistryEntryDisabler.USER


@pytest.mark.parametrize("conflict", ["config_entry", "identifier", "entity"])
async def test_shared_gateway_is_left_untouched(hass, entry, door, gateway, conflict):
    devices, entities = dr.async_get(hass), er.async_get(hass)
    opener_row = registered(hass, entry, door)
    gateway_row = registered(hass, entry, gateway)
    other = MockConfigEntry(domain="unrelated")
    other.add_to_hass(hass)
    if conflict == "config_entry":
        devices.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={(DOMAIN, gateway.key)}
        )
    elif conflict == "identifier":
        devices.async_update_device(
            gateway_row.id,
            new_identifiers=gateway_row.identifiers | {("unrelated", "other-device")},
        )
    else:
        entities.async_get_or_create(
            "sensor", "unrelated", "unrelated-sensor", config_entry=other, device_id=gateway_row.id
        )
    before = dict(devices.devices)
    assert not async_merge_opener_gateway(hass, entry, door, gateway)
    assert dict(devices.devices) == before
    assert devices.async_get_device(identifiers={(DOMAIN, door.key)}).id == opener_row.id


async def test_cross_account_pair_is_rejected(hass, entry, door, gateway):
    assert not async_merge_opener_gateway(
        hass, entry, door, replace(gateway, account_id="another-account")
    )
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)

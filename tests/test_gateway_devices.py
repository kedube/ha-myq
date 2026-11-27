"""Group only an explicitly linked Wi-Fi opener and its built-in gateway."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.myq.api.associations import associations, wifi_gdo_pairs
from custom_components.myq.api.models import Device
from custom_components.myq.const import DOMAIN
from custom_components.myq.panel import DATA_PANEL

from .ha import MockConfigEntry, registry_device

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def inventory(*devices):
    return {device.key: device for device in devices}


def pair():
    opener = Device.parse(
        "account-one",
        {
            "serial_number": "OPENER-1",
            "name": "Garage door",
            "device_type": "wifigaragedooropener",
            "device_family": "garagedoor",
            "device_model": "Synthetic opener model",
            "gateway_id": "GATEWAY-1",
            "state": {"online": True, "door_state": "closed", "firmware_version": "OPENER-FW"},
        },
    )
    gateway = Device.parse(
        "account-one",
        {
            "serial_number": "GATEWAY-1",
            "name": "Garage gateway",
            "device_type": "wifigdogateway",
            "device_family": "gateway",
            "device_model": "Synthetic gateway model",
            "state": {"online": False, "wifi_signal_strength": 66, "firmware_version": "GW-FW"},
        },
    )
    return opener, gateway


@pytest.mark.parametrize(
    "relationship", ["parent_device_id", "gateway_id", "gatewayID", "parent", "reverse"]
)
def test_explicit_wifi_gateway_relationships(relationship):
    opener, gateway = pair()
    if relationship == "parent_device_id":
        opener = Device.parse(
            opener.account_id,
            {
                "serial_number": opener.serial,
                "device_type": opener.device_type,
                "parent_device_id": gateway.serial,
            },
        )
    elif relationship == "gatewayID":
        opener = Device.parse(
            opener.account_id,
            {
                "serial_number": opener.serial,
                "device_type": opener.device_type,
                "state": {"gatewayID": gateway.serial},
            },
        )
    elif relationship == "parent":
        opener = replace(opener, gateway_id=None, state={"parent_serial_number": gateway.serial})
    elif relationship == "reverse":
        opener = replace(opener, gateway_id=None)
        gateway = replace(gateway, state={"associated_gdo_serial_numbers": [opener.serial]})
    assert wifi_gdo_pairs(inventory(opener, gateway)) == {gateway.key: opener}


def parent_linked_pair(account="account-one", number=1):
    """Cloud-shaped records: parent lives outside state, names need not be unique."""
    opener = Device.parse(
        account,
        {
            "serial_number": f"OPENER-{number}",
            "parent_device_id": f"GATEWAY-{number}",
            "device_type": "wifigaragedooropener",
            "device_family": "garagedoor",
            "name": "Garage",
            "state": {
                "door_state": "closed",
                "online": True,
                "attached_camera_serial_number": f"CAMERA-{number}",
            },
        },
    )
    gateway = Device.parse(
        account,
        {
            "serial_number": f"GATEWAY-{number}",
            "device_type": "wifigdogateway",
            "device_family": "gateway",
            "name": "Garage",
            "state": {
                "online": True,
                "firmware_version": f"GW-FW-{number}",
                "wifi_signal_strength": 3,
            },
        },
    )
    camera = Device.parse(
        account,
        {
            "serial_number": f"CAMERA-{number}",
            "device_type": "falcon-camera",
            "device_family": "camera",
            "name": "Garage",
            "state": {"online": True},
        },
    )
    return opener, gateway, camera


@pytest.mark.parametrize("value", [None, "", 123, [], {}])
def test_parent_device_id_ignores_absent_or_malformed_values(value):
    opener = Device.parse(
        "account-one",
        {
            "serial_number": "OPENER",
            "device_type": "wifigaragedooropener",
            "parent_device_id": value,
        },
    )
    assert opener.parent_device_id is None
    assert wifi_gdo_pairs(inventory(opener)) == {}


def test_parent_device_id_is_private_and_does_not_change_command_gateway():
    opener, gateway, _ = parent_linked_pair()
    opener = replace(opener, gateway_id="COMMAND-GATEWAY")
    assert opener.parent_device_id == gateway.serial
    assert opener.gateway_id == "COMMAND-GATEWAY"
    assert gateway.serial not in repr(opener)
    assert wifi_gdo_pairs(inventory(opener, gateway)) == {gateway.key: opener}
    assert associations(opener, inventory(opener, gateway))["gateway"] == (gateway,)


def test_parent_device_id_matches_within_account_even_when_names_and_serials_repeat():
    opener_a, gateway_a, camera_a = parent_linked_pair("account-a")
    opener_b, gateway_b, camera_b = parent_linked_pair("account-b")
    devices = inventory(opener_a, gateway_a, camera_a, opener_b, gateway_b, camera_b)
    assert wifi_gdo_pairs(devices) == {gateway_a.key: opener_a, gateway_b.key: opener_b}
    assert wifi_gdo_pairs(inventory(opener_a, gateway_b)) == {}


@pytest.mark.parametrize(
    "case",
    [
        "missing_gateway",
        "same_name_only",
        "another_account",
        "standalone_hub",
        "non_wifi_opener",
        "two_children",
        "missing_reported_child",
        "conflicting_parent",
        "conflicting_reverse",
        "two_reverse_gateways",
    ],
)
def test_unrelated_or_ambiguous_devices_remain_separate(case):
    opener, gateway = pair()
    extra = []
    if case == "missing_gateway":
        assert wifi_gdo_pairs(inventory(opener)) == {}
        return
    if case == "same_name_only":
        opener = replace(opener, gateway_id=None, name=gateway.name)
    elif case == "another_account":
        gateway = replace(gateway, account_id="other-account")
    elif case == "standalone_hub":
        gateway = replace(gateway, device_type="hub")
    elif case == "non_wifi_opener":
        opener = replace(opener, device_type="garagedooropener")
    elif case == "two_children":
        extra = [replace(opener, serial="OTHER-DOOR", device_type="virtualgaragedooropener")]
    elif case == "missing_reported_child":
        gateway = replace(
            gateway, state={"associated_gdo_serial_numbers": [opener.serial, "MISSING-DOOR"]}
        )
    elif case == "conflicting_parent":
        opener = replace(
            opener, gateway_id="ANOTHER-GATEWAY", state={"parent_serial_number": gateway.serial}
        )
    elif case == "conflicting_reverse":
        opener = replace(opener, gateway_id="ANOTHER-GATEWAY")
        gateway = replace(gateway, state={"associated_gdo_serial_numbers": [opener.serial]})
    elif case == "two_reverse_gateways":
        opener = replace(opener, gateway_id=None)
        gateway = replace(gateway, state={"associated_gdo_serial_numbers": [opener.serial]})
        extra = [replace(gateway, serial="OTHER-GATEWAY")]
    assert wifi_gdo_pairs(inventory(opener, gateway, *extra)) == {}


async def load(hass, tokens, devices):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="owner", data={"tokens": tokens.as_dict()})
    entry.add_to_hass(hass)
    with patch("custom_components.myq.api.client.Client.discover", AsyncMock(return_value=devices)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize("upgrade", [False, True])
async def test_top_level_parent_pairs_merge_duplicate_names_and_keep_cameras_separate(
    hass, tokens, upgrade
):
    pairs = [parent_linked_pair(number=number) for number in (1, 2)]
    # Interleave/reverse cloud records so neither order nor names can select a parent.
    devices = inventory(*(device for group in reversed(pairs) for device in reversed(group)))
    assert wifi_gdo_pairs(devices) == {gateway.key: opener for opener, gateway, _ in pairs}
    entry = MockConfigEntry(domain=DOMAIN, unique_id="owner", data={"tokens": tokens.as_dict()})
    entry.add_to_hass(hass)
    registry, entities = dr.async_get(hass), er.async_get(hass)
    old_devices, old_entities = {}, {}
    if upgrade:
        for opener, gateway, camera in pairs:
            for device, domain, suffix in (
                (opener, "cover", "door"),
                (gateway, "sensor", "wifi_signal_strength"),
                (camera, "camera", "recent_image"),
            ):
                row = registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    identifiers={(DOMAIN, device.key)},
                    name=device.name,
                )
                old_devices[device.key] = row.id
                entity = entities.async_get_or_create(
                    domain,
                    DOMAIN,
                    f"{device.key}:{suffix}",
                    config_entry=entry,
                    device_id=row.id,
                    suggested_object_id="saved_" + device.serial.lower().replace("-", "_"),
                )
                old_entities[device.key] = entities.async_update_entity(
                    entity.entity_id, name="Saved custom name", icon="mdi:star"
                )
        assert len(dr.async_entries_for_config_entry(registry, entry.entry_id)) == 6

    with (
        patch("custom_components.myq.api.client.Client.discover", AsyncMock(return_value=devices)),
        patch("custom_components.myq.api.video.Video.details", AsyncMock(return_value={})),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert len(dr.async_entries_for_config_entry(registry, entry.entry_id)) == 4
    assert len(hass.states.async_all("cover")) == 2
    assert set(entry.runtime_data.data.devices) == set(devices)
    for opener, gateway, camera in pairs:
        row = registry_device(registry, entry.entry_id, (DOMAIN, opener.key))
        assert registry_device(registry, entry.entry_id, (DOMAIN, gateway.key)).id == row.id
        assert row.sw_version == gateway.state["firmware_version"]
        camera_row = registry_device(registry, entry.entry_id, (DOMAIN, camera.key))
        assert camera_row.id != row.id and camera_row.via_device_id == row.id
        if upgrade:
            assert row.id == old_devices[opener.key]
            assert registry.async_get(old_devices[gateway.key]) is None
            assert camera_row.id == old_devices[camera.key]
            for device in (opener, gateway, camera):
                original = old_entities[device.key]
                current = entities.async_get(original.entity_id)
                assert current is not None
                assert current.unique_id == original.unique_id
                assert current.name == original.name and current.icon == original.icon
                assert current.device_id == (camera_row.id if device is camera else row.id)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("gateway_first", [False, True])
async def test_linked_pair_is_one_device_with_both_records_and_stable_entity_ids(
    hass, tokens, gateway_first, hass_admin_user
):
    opener, gateway = pair()
    devices = inventory(gateway, opener) if gateway_first else inventory(opener, gateway)
    entry = await load(hass, tokens, devices)
    registry, entities = dr.async_get(hass), er.async_get(hass)
    canonical = registry_device(registry, entry.entry_id, (DOMAIN, opener.key))
    assert canonical is not None
    assert registry_device(registry, entry.entry_id, (DOMAIN, gateway.key)).id == canonical.id
    assert len(dr.async_entries_for_config_entry(registry, entry.entry_id)) == 1
    assert canonical.name == opener.name
    assert canonical.model == opener.model
    assert canonical.sw_version == "OPENER-FW"
    assert set(entry.runtime_data.data.devices) == set(devices)

    unique_ids = (
        ("cover", f"{opener.key}:door"),
        ("binary_sensor", f"{opener.key}:online"),
        ("sensor", f"{opener.key}:device_type"),
        ("sensor", f"{gateway.key}:wifi_signal_strength"),
    )
    before = {key: entities.async_get_entity_id(domain, DOMAIN, key) for domain, key in unique_ids}
    assert all(before.values())
    assert all(
        entities.async_get(entity_id).device_id == canonical.id for entity_id in before.values()
    )
    # The opener's entities stand for the gateway's duplicate roles.
    for domain, suffix in (
        ("binary_sensor", "online"),
        ("sensor", "device_type"),
        ("sensor", "last_update"),
    ):
        assert entities.async_get_entity_id(domain, DOMAIN, f"{gateway.key}:{suffix}") is None
    # One Connectivity entity: connected only while both records are online.
    connectivity = hass.states.get(before[f"{opener.key}:online"])
    assert connectivity.state == "off"
    assert connectivity.attributes["opener_online"] is True
    assert connectivity.attributes["gateway_online"] is False

    panel = hass.data[DATA_PANEL].async_payload(is_admin=True, every_account=True)
    assert len(panel["doors"]) == 1
    roles = panel["doors"][0]["entities"]
    assert roles["online"] == before[f"{opener.key}:online"]
    assert roles["device_type"] == before[f"{opener.key}:device_type"]
    assert roles["wifi_signal_strength"] == before[f"{gateway.key}:wifi_signal_strength"]

    # Device-target services must resolve aliases to one controllable opener.
    with patch.object(entry.runtime_data.management, "rename_accessory", AsyncMock()) as rename:
        await hass.services.async_call(
            DOMAIN,
            "rename_accessory",
            {"device_id": canonical.id, "accessory_serial": "remote", "name": "Wall remote"},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
    rename.assert_awaited_once_with(opener, accessory_serial="remote", name="Wall remote")

    with patch("custom_components.myq.api.client.Client.discover", AsyncMock(return_value=devices)):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert registry_device(registry, entry.entry_id, (DOMAIN, opener.key)).id == canonical.id
    assert {
        key: entities.async_get_entity_id(domain, DOMAIN, key) for domain, key in unique_ids
    } == before
    assert len(dr.async_entries_for_config_entry(registry, entry.entry_id)) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_gateway_discovered_later_joins_existing_opener(hass, tokens):
    opener, gateway = pair()
    entry = await load(hass, tokens, inventory(opener))
    registry, entities = dr.async_get(hass), er.async_get(hass)
    original = registry_device(registry, entry.entry_id, (DOMAIN, opener.key))
    cover_id = entities.async_get_entity_id("cover", DOMAIN, f"{opener.key}:door")
    with patch.object(
        entry.runtime_data.client, "discover", AsyncMock(return_value=inventory(opener, gateway))
    ):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    assert registry_device(registry, entry.entry_id, (DOMAIN, gateway.key)).id == original.id
    assert registry.async_get(original.id).name == opener.name
    assert len(dr.async_entries_for_config_entry(registry, entry.entry_id)) == 1
    assert entities.async_get_entity_id("cover", DOMAIN, f"{opener.key}:door") == cover_id
    diagnostic_id = entities.async_get_entity_id(
        "sensor", DOMAIN, f"{gateway.key}:wifi_signal_strength"
    )
    assert entities.async_get(diagnostic_id).device_id == original.id
    assert entities.async_get_entity_id("sensor", DOMAIN, f"{gateway.key}:device_type") is None
    # The opener's existing Connectivity entity now follows the gateway too.
    connectivity = entities.async_get_entity_id("binary_sensor", DOMAIN, f"{opener.key}:online")
    assert hass.states.get(connectivity).state == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_gateway_seen_alone_loses_duplicates_when_its_opener_joins(hass, tokens):
    opener, gateway = pair()
    entry = await load(hass, tokens, inventory(gateway))
    entities = er.async_get(hass)
    alone = {
        suffix: entities.async_get_entity_id(domain, DOMAIN, f"{gateway.key}:{suffix}")
        for domain, suffix in (("binary_sensor", "online"), ("sensor", "device_type"))
    }
    assert all(alone.values())
    with patch.object(
        entry.runtime_data.client, "discover", AsyncMock(return_value=inventory(opener, gateway))
    ):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    for entity_id in alone.values():
        assert entities.async_get(entity_id) is None
        assert hass.states.get(entity_id) is None
    online = entities.async_get_entity_id("binary_sensor", DOMAIN, f"{opener.key}:online")
    assert hass.states.get(online).attributes["gateway_online"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_merged_connectivity_exists_when_only_the_gateway_reports_online(hass, tokens):
    opener, gateway = pair()
    # Device.p1 reads an absent opener "online" as online.
    opener = replace(opener, state={"door_state": "closed"})
    entry = await load(hass, tokens, inventory(opener, gateway))
    entities = er.async_get(hass)
    online = entities.async_get_entity_id("binary_sensor", DOMAIN, f"{opener.key}:online")
    state = hass.states.get(online)
    assert state.state == "off"
    assert state.attributes == {
        **state.attributes,
        "opener_online": True,
        "gateway_online": False,
    }
    assert entities.async_get_entity_id("binary_sensor", DOMAIN, f"{gateway.key}:online") is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_upgrade_moves_existing_gateway_entities_without_recreating_them(hass, tokens):
    opener, gateway = pair()
    entry = MockConfigEntry(domain=DOMAIN, unique_id="owner", data={"tokens": tokens.as_dict()})
    entry.add_to_hass(hass)
    registry, entities = dr.async_get(hass), er.async_get(hass)
    opener_row = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, opener.key)}, name=opener.name
    )
    gateway_row = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, gateway.key)}, name=gateway.name
    )
    originals = []
    duplicates = []
    for domain, key, suffix, object_id, disabled in (
        ("cover", opener.key, "door", "my_saved_garage", None),
        (
            "sensor",
            gateway.key,
            "wifi_signal_strength",
            "my_saved_gateway_signal",
            er.RegistryEntryDisabler.USER,
        ),
        ("binary_sensor", gateway.key, "online", "my_saved_gateway_online", None),
        ("sensor", gateway.key, "device_type", "my_saved_gateway_type", None),
    ):
        entity = entities.async_get_or_create(
            domain,
            DOMAIN,
            f"{key}:{suffix}",
            config_entry=entry,
            device_id=opener_row.id if key == opener.key else gateway_row.id,
            suggested_object_id=object_id,
            disabled_by=disabled,
        )
        updated = entities.async_update_entity(
            entity.entity_id, name="Custom " + suffix, icon="mdi:star"
        )
        (duplicates if suffix in {"online", "device_type"} else originals).append(updated)
    with patch(
        "custom_components.myq.api.client.Client.discover",
        AsyncMock(return_value=inventory(opener, gateway)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        # Device removal events must not remove the migrated entities after setup.
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert registry.async_get(gateway_row.id) is None
    assert registry_device(registry, entry.entry_id, (DOMAIN, opener.key)).id == opener_row.id
    assert registry_device(registry, entry.entry_id, (DOMAIN, gateway.key)).id == opener_row.id
    for original in originals:
        migrated = entities.async_get(original.entity_id)
        assert migrated is not None
        assert migrated.device_id == opener_row.id
        assert migrated.unique_id == original.unique_id
        assert migrated.name == original.name
        assert migrated.icon == original.icon
        assert migrated.disabled_by == original.disabled_by
    # The gateway's copies of opener roles are dropped; the opener's entities remain.
    for duplicate in duplicates:
        assert entities.async_get(duplicate.entity_id) is None
        assert hass.states.get(duplicate.entity_id) is None
    assert hass.states.get("cover.my_saved_garage").state == "closed"
    assert hass.states.get("sensor.my_saved_gateway_signal") is None
    online = entities.async_get_entity_id("binary_sensor", DOMAIN, f"{opener.key}:online")
    assert hass.states.get(online).state == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_partial_inventory_does_not_replace_opener_metadata_on_reload(hass, tokens):
    opener, gateway = pair()
    entry = await load(hass, tokens, inventory(opener, gateway))
    registry = dr.async_get(hass)
    original = registry_device(registry, entry.entry_id, (DOMAIN, opener.key))
    with patch(
        "custom_components.myq.api.client.Client.discover",
        AsyncMock(return_value=inventory(gateway)),
    ):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    current = registry_device(registry, entry.entry_id, (DOMAIN, gateway.key))
    assert current.id == original.id
    assert current.name == opener.name
    assert current.model == opener.model
    assert current.sw_version == "OPENER-FW"
    assert len(dr.async_entries_for_config_entry(registry, entry.entry_id)) == 1

    with patch.object(
        entry.runtime_data.client, "discover", AsyncMock(return_value=inventory(opener, gateway))
    ):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    assert registry_device(registry, entry.entry_id, (DOMAIN, opener.key)).id == original.id
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_merged_device_uses_gateway_firmware_when_opener_has_none(hass, tokens):
    opener, gateway = pair()
    opener = replace(
        opener,
        state={key: value for key, value in opener.state.items() if key != "firmware_version"},
    )
    entry = await load(hass, tokens, inventory(opener, gateway))
    registered = registry_device(dr.async_get(hass), entry.entry_id, (DOMAIN, opener.key))
    assert registered.name == opener.name
    assert registered.sw_version == "GW-FW"
    assert await hass.config_entries.async_unload(entry.entry_id)

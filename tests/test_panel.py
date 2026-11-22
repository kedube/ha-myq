"""The restored UI uses this integration's entities, camera links and HA permissions."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.const import DOMAIN
from custom_components.myq.panel import DATA_PANEL, STORAGE_KEY, MyQPanel

from .test_home_assistant import setup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def client(hass, hass_ws_client, inventory):
    return await hass_ws_client(hass)


@pytest.fixture
async def inventory(hass, tokens, door, camera_device, camera_details):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="driver@example.invalid",
        unique_id="panel-test",
        data={"tokens": tokens.as_dict()},
        options={"panel_enabled": True},
    )
    entry.add_to_hass(hass)
    door.state.update(
        attached_camera_serial_number=camera_device.serial,
        active_fault_codes=[{"code": 42, "description": "Synthetic fault"}],
    )
    # Equivalent to frontend's module manager, without loading the whole HA UI.
    hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(lambda action, url: None, [])
    await setup(hass, entry, door, camera_device, camera_details)
    return entry, door, camera_device


async def call(client, message):
    await client.send_json_auto_id(message)
    events = []
    while True:
        response = await client.receive_json()
        if response["type"] == "result" and response["id"] == message["id"]:
            return response, events
        events.append(response["event"])


async def test_inventory_maps_current_entities_and_associated_camera(hass, inventory):
    entry, door, camera = inventory
    panel = hass.data[DATA_PANEL]
    payload = panel.async_payload(is_admin=True)
    assert len(payload["doors"]) == 1
    assert payload["cameras"] == []
    entities = payload["doors"][0]["entities"]
    registry = er.async_get(hass)
    for role, domain, key in (
        ("cover", "cover", f"{door.key}:door"),
        ("camera", "camera", f"{camera.key}:recent_image"),
        ("active_fault", "binary_sensor", f"{door.key}:faults"),
        ("absolute_cycle_count", "sensor", f"{door.key}:absolute_cycle_count"),
    ):
        assert entities[role] == registry.async_get_entity_id(domain, DOMAIN, key)
    assert payload["accounts"] == [
        {"entry_id": entry.entry_id, "title": entry.title, "state": "loaded"}
    ]

    # Renames and disabled entities are read from the registry, not guessed from names.
    registry.async_update_entity(entities["cover"], new_entity_id="cover.renamed_door")
    registry.async_update_entity(entities["camera"], disabled_by=er.RegistryEntryDisabler.USER)
    payload = panel.async_payload(is_admin=False)
    assert payload["doors"][0]["entities"]["cover"] == "cover.renamed_door"
    assert "camera" not in payload["doors"][0]["entities"]
    assert "title" not in payload["accounts"][0]


async def test_separate_camera_and_read_permissions(hass, inventory):
    _, _, camera = inventory
    panel = hass.data[DATA_PANEL]
    camera_device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, camera.key)})
    dr.async_get(hass).async_update_device(camera_device.id, via_device_id=None)
    payload = panel.async_payload(is_admin=False)
    assert payload["cameras"][0]["device_id"] == camera_device.id
    assert "camera" not in payload["doors"][0]["entities"]
    camera_id = payload["cameras"][0]["entity_id"]
    restricted = panel.async_payload(is_admin=False, can_read=lambda eid: eid == camera_id)
    assert restricted["doors"] == []
    assert restricted["cameras"] == payload["cameras"]
    assert panel.async_payload(is_admin=False, can_read=lambda eid: False)["cameras"] == []


async def test_sidebar_assets_options_and_lifecycle(hass, inventory, hass_client):
    entry, _, _ = inventory
    registration = hass.data[frontend.DATA_PANELS][DOMAIN]
    module = registration.config["_panel_custom"]["module_url"]
    assert module.startswith("/myq_panel/")
    modules = hass.data[frontend.DATA_EXTRA_MODULE_URL].urls
    assert module.replace("myq-panel.js", "myq-card.js") in modules
    http = await hass_client()
    for filename in ("myq-panel.js", "door-scene.js", "i18n/en.json"):
        response = await http.get(module.replace("myq-panel.js", filename))
        assert response.status == 200
        assert await response.text()

    with patch.object(hass.config_entries, "async_reload") as reload:
        hass.config_entries.async_update_entry(
            entry,
            options={
                "panel_enabled": True,
                "panel_title": "Garage",
                "panel_icon": "mdi:garage",
                "panel_require_admin": True,
            },
        )
        await hass.async_block_till_done()
        reload.assert_not_called()
    registration = hass.data[frontend.DATA_PANELS][DOMAIN]
    assert registration.sidebar_title == "Garage"
    assert registration.sidebar_icon == "mdi:garage"
    assert registration.require_admin
    assert registration.config["_panel_custom"]["module_url"] == module
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data[frontend.DATA_PANELS]
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data[frontend.DATA_PANELS]


async def test_sidebar_scope_and_dashboard_scope(hass, inventory):
    entry, _, _ = inventory
    panel = hass.data[DATA_PANEL]
    hass.config_entries.async_update_entry(entry, options={"panel_enabled": False})
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data[frontend.DATA_PANELS]
    assert panel.async_payload(is_admin=True)["doors"] == []
    assert len(panel.async_payload(is_admin=True, every_account=True)["doors"]) == 1


async def test_websocket_updates_persist_and_reset_appearance(
    hass, inventory, client, hass_storage
):
    response, _ = await call(client, {"type": "myq/panel/subscribe"})
    assert response["success"]
    initial = (await client.receive_json())["event"]
    device_id = initial["doors"][0]["device_id"]
    appearance = {"style": "carriage", "color": "#112233", "hardware": True}
    response, events = await call(
        client, {"type": "myq/panel/save_appearance", "appearance": {device_id: appearance}}
    )
    assert response["success"]
    assert events[-1]["appearance"] == {device_id: appearance}
    settings = {"confirm": True, "open_warning": 15, "order": [device_id]}
    response, events = await call(client, {"type": "myq/panel/save_settings", "settings": settings})
    assert response["success"]
    assert events[-1]["settings"] == settings
    assert hass_storage[STORAGE_KEY]["data"]["appearance"] == {device_id: appearance}
    restored = MyQPanel(hass)
    # The HTTP server is already running in this test; simulate the next
    # startup's asset registration while exercising the real saved Store.
    with patch.object(restored, "_async_serve_frontend", AsyncMock(return_value=True)):
        await restored.async_setup()
    assert restored.async_payload(is_admin=True)["appearance"] == {device_id: appearance}

    response, events = await call(
        client, {"type": "myq/panel/save_appearance", "appearance": {device_id: None}}
    )
    assert response["success"]
    assert events[-1]["appearance"] == {}


async def test_websocket_inventory_follows_registry_changes(hass, inventory, client):
    response, _ = await call(client, {"type": "myq/panel/subscribe"})
    assert response["success"]
    initial = (await client.receive_json())["event"]
    door = initial["doors"][0]
    dr.async_get(hass).async_update_device(door["device_id"], name_by_user="Renamed garage")
    update = (await client.receive_json())["event"]
    assert update["doors"][0]["name"] == "Renamed garage"


@pytest.mark.parametrize("value", [True, -1, 1441, 7.5, "30"])
async def test_websocket_rejects_invalid_warning(hass, inventory, client, value):
    response, _ = await call(
        client, {"type": "myq/panel/save_settings", "settings": {"open_warning": value}}
    )
    assert response["error"]["code"] == "invalid_format"


async def test_read_only_user_can_view_but_cannot_edit(
    hass, inventory, hass_ws_client, hass_read_only_access_token
):
    client = await hass_ws_client(hass, hass_read_only_access_token)
    response, _ = await call(client, {"type": "myq/panel/subscribe"})
    assert response["success"]
    payload = (await client.receive_json())["event"]
    assert "title" not in payload["accounts"][0]
    device_id = payload["doors"][0]["device_id"]
    for message in (
        {"type": "myq/panel/save_appearance", "appearance": {device_id: {"style": "glass"}}},
        {"type": "myq/panel/save_settings", "settings": {"confirm": False}},
    ):
        response, _ = await call(client, message)
        assert response["error"]["code"] == "unauthorized"

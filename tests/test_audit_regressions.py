"""Home Assistant layer defects found by review; each was reproduced before its fix.

Synthetic devices and patched cloud reads only.
"""

import asyncio
import copy
import logging
import math
import os
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq import async_remove_config_entry_device
from custom_components.myq.api.errors import CannotConnect, CommandRejected
from custom_components.myq.api.models import Device
from custom_components.myq.const import DOMAIN
from custom_components.myq.services import _read_pin

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def new_entry(hass, tokens, **data):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="audit-user", data={"tokens": tokens.as_dict(), **data}
    )
    entry.add_to_hass(hass)
    return entry


def discovered(devices):
    return patch(
        "custom_components.myq.api.client.Client.discover", AsyncMock(return_value=devices)
    )


async def test_renamed_and_disabled_entities_are_not_offered_again_on_every_poll(
    hass, tokens, door, caplog
):
    """Home Assistant runs an entity's removal callbacks for a rename and for a disabled
    entity; treating that as "gone" re-submitted the entity on every poll."""
    entry = new_entry(hass, tokens)
    aborted = []
    abort = Entity.add_to_platform_abort

    def record(self):
        aborted.append(self.unique_id)
        abort(self)

    with discovered({door.key: door}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # What Settings > Entities > "Entity ID" does.
        er.async_get(hass).async_update_entity(
            "cover.test_garage", new_entity_id="cover.garage_door"
        )
        await hass.async_block_till_done()
        assert hass.states.get("cover.garage_door").state == "closed"
        caplog.clear()
        with patch.object(Entity, "add_to_platform_abort", record):
            for _ in range(2):
                await entry.runtime_data.async_refresh()
                await hass.async_block_till_done()
    assert "does not generate unique IDs" not in caplog.text
    assert not [row for row in caplog.records if row.levelno >= logging.ERROR]
    # Entities that are disabled by default were being built, offered and refused too.
    assert aborted == []
    assert hass.states.get("cover.garage_door").state == "closed"


def opener_and_gateway():
    opener = Device.parse(
        "account-one",
        {
            "serial_number": "OPENER-1",
            "name": "Garage door",
            "device_type": "wifigaragedooropener",
            "device_family": "garagedoor",
            "gateway_id": "GATEWAY-1",
            "state": {"online": True, "door_state": "closed"},
        },
    )
    gateway = Device.parse(
        "account-one",
        {
            "serial_number": "GATEWAY-1",
            "name": "Garage gateway",
            "device_type": "wifigdogateway",
            "device_family": "gateway",
            "state": {"online": True, "wifi_signal_strength": 66},
        },
    )
    return opener, gateway


async def test_helper_entity_on_a_merged_opener_keeps_device_services_working(
    hass, tokens, hass_admin_user
):
    opener, gateway = opener_and_gateway()
    entry = new_entry(hass, tokens)
    with discovered({opener.key: opener, gateway.key: gateway}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, opener.key)})
        assert device.identifiers == {(DOMAIN, opener.key), (DOMAIN, gateway.key)}
        # A template, threshold or derivative helper is linked to the opener's device.
        helper = MockConfigEntry(domain="template", title="helper")
        helper.add_to_hass(hass)
        er.async_get(hass).async_get_or_create(
            "binary_sensor", "template", "left-open", config_entry=helper, device_id=device.id
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.gateway_openers == {gateway.key: opener.key}
        rename = AsyncMock()
        with patch.object(coordinator.management, "rename_device", rename):
            await hass.services.async_call(
                DOMAIN,
                "rename_device",
                {"device_id": device.id, "name": "Garage"},
                blocking=True,
                context=Context(user_id=hass_admin_user.id),
            )
        rename.assert_awaited_once()
        assert rename.await_args.args[0].key == opener.key


@pytest.fixture
async def camera_setup(hass, tokens, door, camera_device, camera_details):
    """A loaded entry with one door and one camera; ``details`` is the camera read."""
    entry = new_entry(hass, tokens)
    old = {**copy.deepcopy(camera_details), "alias": "owner:camera"}
    details = AsyncMock(return_value=old)
    discover = AsyncMock(return_value={door.key: door, camera_device.key: camera_device})
    with (
        patch("custom_components.myq.api.client.Client.discover", discover),
        patch("custom_components.myq.api.video.Video.details", details),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry.runtime_data, camera_device.key, old, details, discover


async def test_status_push_survives_a_poll_already_in_flight(hass, camera_setup):
    coordinator, key, old, details, _ = camera_setup
    new = copy.deepcopy(old)
    new["settings"]["cam"]["motionEnabled"] = False  # What the camera's push announced.
    gate = asyncio.Event()

    async def slow_roles():
        await gate.wait()
        return {}

    coordinator._access_cache = None
    coordinator._last_full = -math.inf
    with patch.object(coordinator.management, "account_access", slow_roles):
        poll = hass.async_create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        details.return_value = new
        await coordinator._refresh_camera(key)
        gate.set()
        await poll
    # The poll began before the push and carried the older details over.
    assert coordinator.data.cameras[key]["settings"]["cam"]["motionEnabled"] is False


async def test_setting_change_survives_a_poll_during_the_command(hass, camera_setup):
    coordinator, key, old, details, _ = camera_setup
    new = copy.deepcopy(old)
    new["settings"]["mic"]["enabled"] = False
    started, landed = asyncio.Event(), asyncio.Event()

    async def write_setting():
        """Stands in for a camera setting write, which first waits for the camera to wake."""
        started.set()
        await landed.wait()
        details.return_value = new

    command = hass.async_create_task(coordinator.execute(write_setting()))
    await started.wait()
    coordinator._last_full = -math.inf
    await coordinator.async_refresh()  # The scheduled poll falls due meanwhile.
    landed.set()
    await command
    await hass.async_block_till_done()
    # The command's own refresh must not trust what that poll read before the change.
    assert coordinator.data.cameras[key]["settings"]["mic"]["enabled"] is False


async def test_read_only_service_drops_no_cache_and_forces_no_poll(
    hass, camera_setup, door, hass_admin_user
):
    coordinator, _, _, details, discover = camera_setup
    device_id = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, door.key)}).id
    roles = coordinator.management.account_access
    before = (discover.await_count, details.await_count, roles.await_count)
    with patch.object(coordinator.management, "require_access", AsyncMock()):
        response = await hass.services.async_call(
            DOMAIN,
            "get_alerts",
            {"device_id": device_id},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_admin_user.id),
        )
    await hass.async_block_till_done()
    assert response is not None
    assert (discover.await_count, details.await_count, roles.await_count) == before


async def test_device_removal_is_refused_while_the_entry_is_not_loaded(hass, tokens, door):
    entry = new_entry(hass, tokens)
    with discovered({door.key: door}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, door.key)})
    with patch(
        "custom_components.myq.api.client.Client.discover",
        AsyncMock(side_effect=CannotConnect("down")),
    ):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    # MyQ's current device list is unknown: keep the device rather than raise.
    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_unreadable_saved_session_asks_for_a_new_sign_in(hass, tokens):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="audit-user",
        data={"tokens": {**tokens.as_dict(), "from_another_release": 1}},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


def test_pin_file_that_is_a_directory_leaks_no_descriptor(tmp_path):
    (tmp_path / "folder").mkdir()
    descriptors = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
    before = len(os.listdir(descriptors))
    for _ in range(20):
        with pytest.raises(CommandRejected, match="PIN file must be"):
            _read_pin(str(tmp_path), "folder")
    assert len(os.listdir(descriptors)) == before

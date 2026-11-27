"""Coordinator polling cadence, caches and device lifecycle in real Home Assistant."""

import math
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.myq import async_remove_config_entry_device
from custom_components.myq import coordinator as coordinator_module
from custom_components.myq.api.account_access import parse_account_access
from custom_components.myq.api.errors import CannotConnect, RateLimited
from custom_components.myq.api.http import Response
from custom_components.myq.api.management import Accessory
from custom_components.myq.api.models import Device
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import (
    CAMERA_TTL,
    FAST_POLL_SECONDS,
    MISSING_POLLS_BEFORE_REMOVAL,
    track_seconds,
)

from .ha import MockConfigEntry, registry_device
from .test_home_assistant import setup
from .test_management import access_payload

NORMAL = timedelta(seconds=60)
FAST = timedelta(seconds=FAST_POLL_SECONDS)


@pytest.fixture(autouse=True)
def allow_custom(enable_custom_integrations):
    yield


@pytest.fixture
def just_booted(monkeypatch):
    """Run the coordinator on a clock that started five seconds ago.

    time.monotonic() counts from boot, so a fresh CI runner, or a host that has just
    restarted, reads a small number. A cache must still treat data it never read as stale.
    """
    start = time.monotonic() - 5
    clock = SimpleNamespace(monotonic=lambda: time.monotonic() - start)
    monkeypatch.setattr(coordinator_module, "time", clock)


@pytest.fixture
def entry(hass, tokens):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    return entry


def with_state(device: Device, payload: dict, **state) -> Device:
    return Device.parse(device.account_id, {**payload, "state": {**payload["state"], **state}})


async def open_cover(hass, coordinator, door):
    cover_id = hass.states.async_all("cover")[0].entity_id
    with patch.object(
        coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
    ):
        await hass.services.async_call(
            "cover", "open_cover", {"entity_id": cover_id}, blocking=True
        )
        await hass.async_block_till_done()
    return cover_id


class Cloud:
    """Serve one device set to full discovery and to one-account follow-up reads."""

    def __init__(self, *devices: Device) -> None:
        self.devices = {device.key: device for device in devices}
        self.discover = AsyncMock(side_effect=lambda **_: dict(self.devices))
        self.account = AsyncMock(side_effect=self._account)

    def _account(self, account_id: str) -> list[Device]:
        return [device for device in self.devices.values() if device.account_id == account_id]

    def set(self, *devices: Device) -> None:
        self.devices.update({device.key: device for device in devices})

    def fail(self, error: Exception) -> None:
        self.discover.side_effect = self.account.side_effect = error


@contextmanager
def cloud(coordinator, *devices: Device):
    fake = Cloud(*devices)
    with (
        patch.object(coordinator.client, "discover", fake.discover),
        patch.object(coordinator.client, "devices", fake.account),
    ):
        yield fake


def elapse(coordinator, key: str, seconds: float) -> None:
    """Move one tracked device's window as if this much time had passed."""
    tracked = coordinator._tracking[key]
    coordinator._tracking[key] = replace(tracked, until=tracked.until - seconds)


def expire(coordinator, key: str) -> None:
    coordinator._tracking[key] = replace(coordinator._tracking[key], until=time.monotonic() - 1)


async def test_door_command_polls_one_account_until_the_door_is_open(
    hass, entry, door, door_payload
):
    coordinator = await setup(hass, entry, door)
    assert coordinator.update_interval == NORMAL
    with (
        cloud(coordinator, door) as myq,
        patch.object(coordinator.garage, "read", AsyncMock(return_value={})) as settings,
    ):
        cover_id = await open_cover(hass, coordinator, door)
        assert coordinator.update_interval == FAST
        # Like the app's poll: one account's device list, no account list or settings.
        myq.account.assert_awaited_once_with("test-account")
        myq.set(with_state(door, door_payload, door_state="opening"))
        await coordinator.async_refresh()
        assert hass.states.get(cover_id).state == "opening"
        assert coordinator.update_interval == FAST
        myq.set(with_state(door, door_payload, door_state="open"))
        await coordinator.async_refresh()
        assert coordinator.update_interval == NORMAL  # The target state ends tracking.
        assert hass.states.get(cover_id).state == "open"
        myq.discover.assert_not_awaited()
        settings.assert_not_awaited()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_tracking_waits_through_the_warning_before_a_door_moves(
    hass, entry, door, door_payload
):
    """An unattended close beeps before moving, so "open" can persist after the command."""
    opened = with_state(door, door_payload, door_state="open")
    coordinator = await setup(hass, entry, opened)
    cover_id = hass.states.async_all("cover")[0].entity_id
    with (
        cloud(coordinator, opened) as myq,
        patch.object(
            coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
        ),
    ):
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": cover_id}, blocking=True
        )
        for _ in range(4):  # Still "open" 48 s after the command; the window is 60 s.
            elapse(coordinator, door.key, 12)
            await coordinator.async_refresh()
            assert coordinator.update_interval == FAST
        myq.set(with_state(door, door_payload, door_state="closing"))
        await coordinator.async_refresh()
        assert coordinator.update_interval == FAST
        myq.set(with_state(door, door_payload, door_state="closed"))
        await coordinator.async_refresh()
        assert coordinator.update_interval == NORMAL
    await hass.config_entries.async_unload(entry.entry_id)


async def test_tracking_ends_at_its_window_and_on_failure(hass, entry, door, door_payload):
    coordinator = await setup(hass, entry, door)
    with cloud(coordinator, with_state(door, door_payload, door_state="closing")) as myq:
        await open_cover(hass, coordinator, door)
        assert coordinator.update_interval == FAST
        expire(coordinator, door.key)  # A door stuck in "closing".
        await coordinator.async_refresh()
        assert coordinator.update_interval == NORMAL
        await open_cover(hass, coordinator, door)
        assert coordinator.update_interval == FAST
        myq.fail(CannotConnect("lost"))
        await coordinator.async_refresh()
        assert coordinator.update_interval == NORMAL
        assert not coordinator._tracking
        myq.fail(RateLimited(120))
        coordinator.track(door.key, "open")
        await coordinator.async_refresh()
        assert coordinator.update_interval == NORMAL
        assert coordinator._retry_after is None  # Consumed when scheduling the next poll.
    await hass.config_entries.async_unload(entry.entry_id)


async def test_movement_seen_in_a_poll_is_tracked_once(hass, entry, door, door_payload):
    """A wall-button movement is followed to its target; a stuck state cannot re-arm it."""
    coordinator = await setup(hass, entry, door)
    with cloud(coordinator, with_state(door, door_payload, door_state="opening")) as myq:
        await coordinator.async_refresh()
        assert coordinator.update_interval == FAST
        assert coordinator._tracking[door.key].target == "open"
        expire(coordinator, door.key)
        await coordinator.async_refresh()
        assert coordinator.update_interval == NORMAL
        await coordinator.async_refresh()
        assert coordinator.update_interval == NORMAL  # Same "opening" report: no re-arm.
        myq.set(with_state(door, door_payload, door_state="closing"))
        await coordinator.async_refresh()
        assert coordinator._tracking[door.key].target == "closed"
        assert coordinator.update_interval == FAST
    await hass.config_entries.async_unload(entry.entry_id)


async def test_follow_up_leaves_other_accounts_unread(hass, entry, door):
    payload = {
        "serial_number": "LAMP-2",
        "name": "Porch lamp",
        "device_type": "lamp",
        "device_family": "lamp",
        "state": {"online": True, "lamp_state": "off"},
    }
    lamp = Device.parse("other-account", payload)
    coordinator = await setup(hass, entry, door)
    with cloud(coordinator, door, lamp) as myq:
        await coordinator.async_refresh()
        myq.set(with_state(lamp, payload, lamp_state="on"))
        await open_cover(hass, coordinator, door)
        myq.account.assert_awaited_once_with("test-account")
        assert coordinator.data.devices[lamp.key].state["lamp_state"] == "off"  # Carried over.
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "device_type,family,seconds",
    [
        ("garagedooropener", "garagedoor", 60),
        ("wifigaragedooropener", "garagedoor", 60),
        ("virtualgaragedooropener", "garagedoor", 90),
        ("gate", "garagedoor", 120),
        ("commercialdooropener", "garagedoor", 120),
        ("wifilockgateway", "locks", 30),
        ("vendor_lock", "locks", 30),
        ("lamp", "lamp", 60),
    ],
)
def test_tracking_windows_follow_the_app(device_type, family, seconds):
    device = Device.parse(
        "a",
        {"serial_number": "S", "device_type": device_type, "device_family": family, "state": {}},
    )
    assert track_seconds(device) == seconds


async def test_settings_changes_do_not_start_fast_polling(hass, entry, door):
    coordinator = await setup(hass, entry, door)
    with patch.object(coordinator.client, "discover", AsyncMock(return_value={door.key: door})):
        await coordinator.execute(AsyncMock()())
    assert coordinator.update_interval == NORMAL
    await hass.config_entries.async_unload(entry.entry_id)


async def test_lock_transition_counts_beside_an_open_door_sensor(hass, entry):
    from custom_components.myq.coordinator import transition

    from .test_locks import make_lock

    lock = make_lock(lock_state="locking", door_state="open")
    assert transition(lock) == "locking"


async def test_account_access_is_cached_between_polls_but_not_after_errors(hass, entry, door):
    coordinator = await setup(hass, entry, door)
    grants = parse_account_access(access_payload())
    with (
        patch.object(coordinator.client, "discover", AsyncMock(return_value={door.key: door})),
        patch.object(
            coordinator.management, "account_access", AsyncMock(return_value=grants)
        ) as read,
    ):
        coordinator._access_cache = None
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        read.assert_awaited_once()
        assert coordinator.data.account_access == grants
        await coordinator.execute(AsyncMock()())  # Any command can follow a grant change.
        await coordinator.async_refresh()
        assert read.await_count == 2
        coordinator._access_cache = None
        read.side_effect = CannotConnect("lost")
        await coordinator.async_refresh()
        assert coordinator.data.account_access == {}
        assert coordinator.data.account_access_error == "CannotConnect"
        read.side_effect = None
        await coordinator.async_refresh()
        assert read.await_count == 4  # A failed read is never served from cache.
    await hass.config_entries.async_unload(entry.entry_id)


async def test_device_missing_for_several_polls_is_removed_and_can_return(
    hass, entry, door, door_payload
):
    coordinator = await setup(hass, entry, door)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    registered = registry_device(devices, entry.entry_id, (DOMAIN, door.key))
    other = Device.parse("test-account", {**door_payload, "serial_number": "OTHER"})
    with patch.object(
        coordinator.client, "discover", AsyncMock(return_value={other.key: other})
    ) as discover:
        for _ in range(MISSING_POLLS_BEFORE_REMOVAL - 1):
            await coordinator.async_refresh()
        assert devices.async_get(registered.id) is not None
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert devices.async_get(registered.id) is None
        assert entities.async_get_entity_id("cover", DOMAIN, f"{door.key}:door") is None
        discover.return_value = {door.key: door, other.key: other}
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert entities.async_get_entity_id("cover", DOMAIN, f"{door.key}:door") is not None
    assert len(hass.states.async_all("cover")) == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_empty_inventory_never_prunes_devices(hass, entry, door):
    coordinator = await setup(hass, entry, door)
    devices = dr.async_get(hass)
    with patch.object(coordinator.client, "discover", AsyncMock(return_value={})):
        for _ in range(MISSING_POLLS_BEFORE_REMOVAL + 1):
            await coordinator.async_refresh()
    assert registry_device(devices, entry.entry_id, (DOMAIN, door.key)) is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_manual_device_removal_only_for_devices_myq_no_longer_returns(
    hass, entry, door, door_payload
):
    coordinator = await setup(hass, entry, door)
    devices = dr.async_get(hass)
    registered = registry_device(devices, entry.entry_id, (DOMAIN, door.key))
    assert not await async_remove_config_entry_device(hass, entry, registered)
    other = Device.parse("test-account", {**door_payload, "serial_number": "OTHER"})
    with patch.object(coordinator.client, "discover", AsyncMock(return_value={other.key: other})):
        await coordinator.async_refresh()
    assert await async_remove_config_entry_device(hass, entry, registered)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_enabled_accessory_sensors_resume_polling_after_restart(hass, entry, door):
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{door.key}:accessory_TX02-test_battery", config_entry=entry
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "other-account:OTHER:accessory_TX9_battery",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    remote = Accessory("TX02-test", "Remote", "OokTx", False, "low")
    with (
        patch(
            "custom_components.myq.api.management.Management.account_access",
            AsyncMock(return_value=parse_account_access(access_payload())),
        ),
        patch(
            "custom_components.myq.api.management.Management.accessories",
            AsyncMock(return_value=(remote,)),
        ) as read,
    ):
        coordinator = await setup(hass, entry, door)
        assert coordinator._tracked_accessories == {door.key}
        assert coordinator.data.accessories[door.key] == (remote,)
        battery = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{door.key}:accessory_TX02-test_battery"
        )
        assert hass.states.get(battery).state == "low"
        with patch.object(coordinator.client, "discover", AsyncMock(return_value={door.key: door})):
            await coordinator.async_refresh()
        read.assert_awaited_once()  # The five-minute cache serves the next poll.
        coordinator.invalidate_accessories(door)
        read.side_effect = CannotConnect("lost")
        with patch.object(coordinator.client, "discover", AsyncMock(return_value={door.key: door})):
            await coordinator.async_refresh()
        assert coordinator.data.accessory_errors[door.key] == "CannotConnect"
        assert door.key not in coordinator.data.accessories
    await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostic_copies_of_control_state_start_disabled(hass, entry, door):
    await setup(hass, entry, door)
    registry = er.async_get(hass)
    door_state = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{door.key}:door_state")
    )
    cycles = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{door.key}:absolute_cycle_count")
    )
    assert door_state.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert cycles.disabled_by is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_service_names_come_from_translations(hass, entry, door):
    from homeassistant.helpers.translation import async_get_translations

    await setup(hass, entry, door)
    text = await async_get_translations(hass, "en", "services", {DOMAIN})
    prefix = f"component.{DOMAIN}.services.set_auto_lock"
    assert text[f"{prefix}.name"] == "Set auto lock"
    assert text[f"{prefix}.fields.seconds.description"].startswith("Delay in seconds")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_accounts_list_is_reused_and_fetched_again_after_a_failure(
    hass, just_booted, entry, door
):
    coordinator = await setup(hass, entry, door)
    with cloud(coordinator, door) as myq:
        coordinator._accounts_read_at = -math.inf
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        # fn0/a: the app fetches the accounts list when it starts, not on every poll.
        assert [c.kwargs["reuse_accounts"] for c in myq.discover.await_args_list] == [False, True]
        myq.fail(CannotConnect("lost"))
        await coordinator.async_refresh()
        myq.discover.side_effect = lambda **_: dict(myq.devices)
        await coordinator.async_refresh()
        assert myq.discover.await_args_list[-1].kwargs["reuse_accounts"] is False
    await hass.config_entries.async_unload(entry.entry_id)


async def test_camera_details_are_reused_until_a_command_or_their_ttl(
    hass, just_booted, entry, door, camera_device, camera_details
):
    coordinator = await setup(hass, entry, door, camera_device, camera_details)
    with (
        patch.object(
            coordinator.client,
            "discover",
            AsyncMock(return_value={door.key: door, camera_device.key: camera_device}),
        ),
        patch.object(coordinator.video, "details", AsyncMock(return_value=camera_details)) as read,
    ):
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        read.assert_not_awaited()  # Read at setup moments ago; pushes cover changes.
        await coordinator.execute(AsyncMock()())  # A camera setting shows only when re-read.
        await coordinator.async_refresh()
        assert read.await_count == 1
        coordinator._camera_read_at[camera_device.key] -= CAMERA_TTL
        await coordinator.async_refresh()
        assert read.await_count == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_old_poll_interval_below_the_minimum_is_raised(hass, tokens, door):
    old = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={"tokens": tokens.as_dict()},
        options={"poll_interval": 30},
    )
    old.add_to_hass(hass)
    coordinator = await setup(hass, old, door)
    assert coordinator.poll_interval == timedelta(seconds=60)
    await hass.config_entries.async_unload(old.entry_id)

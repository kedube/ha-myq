"""Use the HA helpers matching each isolated test environment."""

from collections.abc import Mapping
from importlib.util import find_spec

from homeassistant.helpers import device_registry as dr

if find_spec("pytest_homeassistant") is not None:
    from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
else:
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed,
    )

__all__ = ["MockConfigEntry", "async_fire_time_changed"]


def registry_device(registry, entry_id, identifier):
    """Inspect a test entry's devices without deprecated global identifier lookup."""
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, entry_id)
            if identifier in device.identifiers
        ),
        None,
    )


def registry_snapshot(registry):
    """Snapshot all rows across HA 2026.9's change of `devices` from a mapping."""
    rows = registry.devices
    return {device.id: device for device in (rows.values() if isinstance(rows, Mapping) else rows)}

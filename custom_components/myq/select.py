"""Selections derived from native device settings and advertised options."""

from .camera_entities import camera_entities
from .entity import discover_entities
from .garage_entities import garage_select_entities
from .management_entities import management_select_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    def factory(device):
        yield from garage_select_entities(entry.runtime_data, device)
        yield from management_select_entities(entry.runtime_data, device)
        yield from camera_entities(entry.runtime_data, device, "select")

    discover_entities(entry.runtime_data, async_add_entities, factory)

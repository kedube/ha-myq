"""Explicit, bounded live camera actions on an already open view."""

from .camera_controls import camera_control_buttons
from .entity import discover_entities
from .management_entities import management_button_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    discover_entities(
        entry.runtime_data,
        async_add_entities,
        lambda device: [
            *camera_control_buttons(entry.runtime_data, device),
            *management_button_entities(entry.runtime_data, device),
        ],
    )

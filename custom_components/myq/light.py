"""On/off control for standalone MyQ lamp devices observed in the app."""

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity

from .camera_entities import camera_entities
from .entity import MyQEntity, discover_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    def factory(device):
        if device.is_lamp:
            yield MyQLight(entry.runtime_data, device)
        yield from camera_entities(entry.runtime_data, device, "light")

    discover_entities(entry.runtime_data, async_add_entities, factory)


class MyQLight(MyQEntity, LightEntity):
    _attr_name = None
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "lamp")

    @property
    def is_on(self) -> bool | None:
        state = self.device.state.get("lamp_state") if self.device else None
        state = state.lower() if isinstance(state, str) else None
        return state == "on" if state in {"on", "off"} else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.execute(
            self.coordinator.client.lamp(self.device, True), expect=(self.device_key, "on")
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.execute(
            self.coordinator.client.lamp(self.device, False), expect=(self.device_key, "off")
        )

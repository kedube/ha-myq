"""Garage door entities with explicit remote-operation capability checks."""

from typing import Any

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature

from .entity import MyQEntity, discover_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    discover_entities(
        entry.runtime_data,
        async_add_entities,
        lambda device: [MyQCover(entry.runtime_data, device)] if device.is_door else [],
    )


class MyQCover(MyQEntity, CoverEntity):
    _attr_name = None
    _attr_translation_key = "door"

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "door")
        self._attr_device_class = (
            CoverDeviceClass.GATE if device.device_type == "gate" else CoverDeviceClass.GARAGE
        )

    @property
    def supported_features(self) -> CoverEntityFeature:
        features = CoverEntityFeature(0)
        if self.device:
            if self.device.can_move("open"):
                features |= CoverEntityFeature.OPEN
            if self.device.can_move("close"):
                features |= CoverEntityFeature.CLOSE
        return features

    @property
    def is_closed(self) -> bool | None:
        state = self.device.door_state if self.device else None
        return None if state is None else state == "closed"

    @property
    def is_opening(self) -> bool | None:
        state = self.device.door_state if self.device else None
        return None if state is None else state == "opening"

    @property
    def is_closing(self) -> bool | None:
        state = self.device.door_state if self.device else None
        return None if state is None else state == "closing"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        # MyQ's own state: a door stopped partway is "open" to Home Assistant, "stopped" here.
        state = self.device.door_state if self.device else None
        return None if state is None else {"door_state": state}

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.coordinator.execute(
            self.coordinator.client.command(self.device, "open"),
            expect=(self.device_key, "open"),
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.coordinator.execute(
            self.coordinator.client.command(self.device, "close"),
            expect=(self.device_key, "closed"),
        )

"""Camera microphone and motion settings, gated by returned fields."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .camera_entities import camera_entities
from .entity import MyQEntity, discover_entities
from .garage_entities import garage_switch_entities
from .management_entities import management_switch_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    def factory(device):
        yield from garage_switch_entities(entry.runtime_data, device)
        yield from management_switch_entities(entry.runtime_data, device)
        yield from camera_entities(entry.runtime_data, device, "switch")
        data = entry.runtime_data.data.cameras.get(device.key, {})
        settings, cap = data.get("settings", {}), data.get("cap", {})
        if type(settings.get("mic", {}).get("enabled")) is bool and "mic" in cap:
            yield MyQCameraSwitch(entry.runtime_data, device, "mic", "enabled", "microphone")
        if (
            type(settings.get("cam", {}).get("motionEnabled")) is bool
            and cap.get("cam", {}).get("motion") is True
        ):
            yield MyQCameraSwitch(
                entry.runtime_data,
                device,
                "cam",
                "motionEnabled",
                "motion_detection",
            )

    discover_entities(entry.runtime_data, async_add_entities, factory)


class MyQCameraSwitch(MyQEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device, section, key, translation_key) -> None:
        super().__init__(coordinator, device, f"{section}_{key}")
        self.section, self.key = section, key
        self._attr_translation_key = translation_key

    @property
    def available(self) -> bool:
        cap = self.camera_details.get("cap", {})
        supported = (
            isinstance(cap.get("mic"), dict)
            if self.section == "mic"
            else cap.get("cam", {}).get("motion") is True
        )
        return super().available and self.account_owner and supported and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        value = self.camera_details.get("settings", {}).get(self.section, {}).get(self.key)
        return value if type(value) is bool else None

    async def _set(self, value: bool) -> None:
        await self.coordinator.execute(
            self.coordinator.video.setting(
                self.device,
                self.section,
                self.key,
                value,
            )
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

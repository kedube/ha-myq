"""Camera speaker volume using limits advertised by the camera."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory

from .camera_entities import camera_entities
from .entity import MyQEntity, discover_entities
from .exceptions import translated
from .garage_entities import garage_number_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    def factory(device):
        yield from garage_number_entities(entry.runtime_data, device)
        yield from camera_entities(entry.runtime_data, device, "number")
        data = entry.runtime_data.data.cameras.get(device.key, {})
        cap = data.get("cap", {}).get("spk", {})
        if (
            type(cap.get("volMin")) is int
            and type(cap.get("volMax")) is int
            and cap["volMin"] < cap["volMax"]
            and type(data.get("settings", {}).get("spk", {}).get("vol")) is int
        ):
            yield MyQVolume(entry.runtime_data, device)

    discover_entities(entry.runtime_data, async_add_entities, factory)


class MyQVolume(MyQEntity, NumberEntity):
    _attr_translation_key = "speaker_volume"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "speaker_volume")

    @property
    def details(self):
        return self.camera_details

    @property
    def available(self) -> bool:
        cap = self.details.get("cap", {}).get("spk", {})
        return (
            super().available
            and self.account_owner
            and type(cap.get("volMin")) is int
            and type(cap.get("volMax")) is int
            and type(self.native_value) is int
        )

    @property
    def native_min_value(self):
        return self.details.get("cap", {}).get("spk", {}).get("volMin", 0)

    @property
    def native_max_value(self):
        return self.details.get("cap", {}).get("spk", {}).get("volMax", 100)

    @property
    def native_value(self):
        return self.details.get("settings", {}).get("spk", {}).get("vol")

    async def async_set_native_value(self, value: float) -> None:
        if not isinstance(value, (int, float)) or not float(value).is_integer():
            raise translated("whole_number")
        await self.coordinator.execute(
            self.coordinator.video.setting(
                self.device,
                "spk",
                "vol",
                int(value),
            )
        )

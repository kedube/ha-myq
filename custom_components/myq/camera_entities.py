"""Camera configuration entities driven by the returned Tend capabilities."""

from math import isfinite
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import PERCENTAGE, EntityCategory

from .api.camera_settings import choices, light_limits, lights, limits, supported, value
from .api.models import nested
from .entity import MyQEntity
from .exceptions import translated


def video_value(details: dict, key: str):
    raw = nested(details, "settings", "cam", key)
    if key == "res":
        return raw if isinstance(raw, str) and raw else None
    return raw if type(raw) in (int, float) and isfinite(raw) and raw >= 0 else None


# PowerSourceDeserializer values, in HA's lowercase option form.
POWER_SOURCES = {
    "AC": "ac",
    "USB": "usb",
    "BATT": "battery",
    "SOLAR": "solar",
    "SOLAR+BATT": "solar_battery",
}


def _percent(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 100 else None


def status_value(details: dict, key: str) -> Any:
    """Camera power and network status already present in the polled details."""
    if key == "battery":
        # PowerStatus.b: battLv is valid from 0 to 100.
        return _percent(nested(details, "status", "pwr", "battLv"))
    if key == "power_source":
        raw = nested(details, "status", "pwr", "pwrSrc")
        return POWER_SOURCES.get(raw.upper()) if isinstance(raw, str) else None
    if key == "wifi_strength":
        return _percent(nested(details, "net", "eth", "wifiStrength"))
    if key == "charging":
        # PowerStatus.h: charging while battChrgMw is positive.
        raw = nested(details, "status", "pwr", "battChrgMw")
        return raw > 0 if type(raw) in (int, float) and isfinite(raw) else None
    return None


def camera_entities(coordinator, device, platform: str):
    """Yield entities only for concrete capabilities and decodable current state."""
    if not device.is_camera:
        return
    details = coordinator.data.cameras.get(device.key, {})
    keys = {
        "switch": ("status_led", "chime_enabled", "auto_tracking", "wake_on_motion"),
        "number": ("motion_sensitivity", "siren_volume"),
        "select": ("rotation", "night_vision", "power_saver", "chime_type"),
    }
    classes = {"switch": CameraSwitch, "number": CameraNumber, "select": CameraSelect}
    for key in keys.get(platform, ()):
        if supported(details, key):
            yield classes[platform](coordinator, device, key)
    if platform == "light":
        for index in range(len(lights(details))):
            yield CameraLight(coordinator, device, index)
    if platform == "sensor":
        for key in ("res", "fps", "quality"):
            if video_value(details, key) is not None:
                yield CameraVideoSensor(coordinator, device, key)
        for key in ("battery", "power_source", "wifi_strength"):
            if status_value(details, key) is not None:
                yield CameraStatusSensor(coordinator, device, key)
    if platform == "binary_sensor" and status_value(details, "charging") is not None:
        yield CameraCharging(coordinator, device)


class CameraSetting(MyQEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device, key) -> None:
        super().__init__(coordinator, device, "camera_" + key)
        self.key = key
        self._attr_translation_key = key

    @property
    def details(self):
        return self.camera_details

    @property
    def available(self):
        return super().available and self.account_owner and supported(self.details, self.key)

    async def _set(self, desired):
        await self.coordinator.execute(
            self.coordinator.camera_settings.set(self.device, self.key, desired)
        )


class CameraSwitch(CameraSetting, SwitchEntity):
    @property
    def is_on(self):
        return value(self.details, self.key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class CameraNumber(CameraSetting, NumberEntity):
    _attr_mode = NumberMode.SLIDER

    @property
    def native_step(self):
        # The app's siren slider moves in fixed steps of 10 (60 to 100).
        return 10 if self.key == "siren_volume" else 1

    @property
    def native_min_value(self):
        return (limits(self.details, self.key) or (0, 0))[0]

    @property
    def native_max_value(self):
        return (limits(self.details, self.key) or (0, 0))[1]

    @property
    def native_value(self):
        return value(self.details, self.key)

    async def async_set_native_value(self, desired: float) -> None:
        if type(desired) not in (int, float) or not float(desired).is_integer():
            raise translated("whole_number")
        await self._set(int(desired))


class CameraSelect(CameraSetting, SelectEntity):
    @property
    def options(self):
        return list(choices(self.details, self.key))

    @property
    def current_option(self):
        return value(self.details, self.key)

    async def async_select_option(self, option: str) -> None:
        await self._set(option)


class CameraLight(MyQEntity, LightEntity):
    def __init__(self, coordinator, device, index: int) -> None:
        super().__init__(coordinator, device, f"camera_light_{index}")
        self.index = index
        self._attr_translation_key = "camera_light"
        self._attr_translation_placeholders = {"number": str(index + 1)}

    @property
    def details(self):
        return self.camera_details

    @property
    def item(self):
        items = lights(self.details)
        return items[self.index] if self.index < len(items) else ({}, {})

    @property
    def available(self):
        return super().available and bool(self.item[0])

    @property
    def is_on(self):
        return {"on": True, "off": False}.get(self.item[1].get("switchState"))

    @property
    def brightness(self):
        cap, setting = self.item
        if (bounds := light_limits(cap, setting)) is None:
            return None
        low, high = bounds
        return round(1 + (setting["intensity"] - low) * 254 / (high - low))

    @property
    def supported_color_modes(self):
        return {ColorMode.BRIGHTNESS if light_limits(*self.item) else ColorMode.ONOFF}

    @property
    def color_mode(self):
        return next(iter(self.supported_color_modes))

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness == 0:
            await self.async_turn_off()
            return
        await self.coordinator.execute(
            self.coordinator.camera_settings.light(self.device, self.index, True, brightness)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.execute(
            self.coordinator.camera_settings.light(self.device, self.index, False)
        )


class CameraVideoSensor(MyQEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device, key: str) -> None:
        super().__init__(coordinator, device, "camera_video_" + key)
        self.key = key
        self._attr_translation_key = "video_" + key

    @property
    def native_value(self):
        return video_value(self.camera_details, self.key)

    @property
    def available(self):
        return super().available and self.native_value is not None


class CameraStatusSensor(MyQEntity, SensorEntity):
    def __init__(self, coordinator, device, key: str) -> None:
        super().__init__(coordinator, device, "camera_" + key)
        self.key = key
        self._attr_translation_key = "camera_" + key
        if key == "battery":
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "power_source":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(POWER_SOURCES.values())
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        else:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return status_value(self.camera_details, self.key)

    @property
    def available(self):
        return super().available and self.native_value is not None


class CameraCharging(MyQEntity, BinarySensorEntity):
    _attr_translation_key = "camera_charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "camera_charging")

    @property
    def is_on(self):
        return status_value(self.camera_details, "charging")

    @property
    def available(self):
        return super().available and self.is_on is not None

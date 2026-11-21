"""Residential opener configuration entities backed by observed settings."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, UnitOfTime

from .api.garage import LIGHT_TIMER_SECONDS, can_configure_garage
from .entity import MyQEntity
from .exceptions import translated


def _settings(coordinator, device_key):
    return getattr(coordinator.data, "garage_settings", {}).get(device_key, {})


def _configurable(coordinator, device) -> bool:
    access = (
        getattr(coordinator.data, "account_access", {}).get(device.account_id) if device else None
    )
    return can_configure_garage(device, access)


def garage_switch_entities(coordinator, device):
    settings = _settings(coordinator, device.key)
    if _configurable(coordinator, device) and all(
        type(settings.get(key)) is bool for key in ("enable_pir_control", "enable_motion_wl")
    ):
        yield MyQOpenerMotionLight(coordinator, device, "motion_activated_light")


def garage_select_entities(coordinator, device):
    value = _settings(coordinator, device.key).get("light_timer_setting")
    if _configurable(coordinator, device) and type(value) is int and value in LIGHT_TIMER_SECONDS:
        yield MyQOpenerLightTimer(coordinator, device, "light_timer")


def garage_number_entities(coordinator, device):
    value = _settings(coordinator, device.key).get("time_to_close_setting")
    if (
        _configurable(coordinator, device)
        and device.can_move("close")
        and type(value) is int
        and 0 <= value <= 99 * 60
        and value % 60 == 0
    ):
        yield MyQOpenerTimeToClose(coordinator, device, "time_to_close")


class _OpenerSetting(MyQEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def settings(self):
        return _settings(self.coordinator, self.device_key)

    @property
    def available(self) -> bool:
        return super().available and _configurable(self.coordinator, self.device)


class MyQOpenerMotionLight(_OpenerSetting, SwitchEntity):
    _attr_translation_key = "motion_light"
    _attr_icon = "mdi:lightbulb-auto"

    @property
    def is_on(self) -> bool | None:
        values = [self.settings.get(key) for key in ("enable_pir_control", "enable_motion_wl")]
        return any(values) if all(type(value) is bool for value in values) else None

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.execute(
            self.coordinator.garage.set_motion_activation(self.device, True)
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.execute(
            self.coordinator.garage.set_motion_activation(self.device, False)
        )


class MyQOpenerLightTimer(_OpenerSetting, SelectEntity):
    _attr_translation_key = "light_timer"
    _attr_icon = "mdi:lightbulb-on-10"
    _attr_options = [str(seconds) for seconds in LIGHT_TIMER_SECONDS]

    @property
    def current_option(self) -> str | None:
        value = self.settings.get("light_timer_setting")
        return str(value) if type(value) is int and value in LIGHT_TIMER_SECONDS else None

    @property
    def available(self) -> bool:
        return super().available and self.current_option is not None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise translated("unsupported_option")
        seconds = LIGHT_TIMER_SECONDS[self.options.index(option)]
        await self.coordinator.execute(
            self.coordinator.garage.set_light_timer(self.device, seconds)
        )


class MyQOpenerTimeToClose(_OpenerSetting, NumberEntity):
    _attr_translation_key = "time_to_close"
    _attr_icon = "mdi:garage-variant"
    _attr_native_min_value = 0
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> int | None:
        value = self.settings.get("time_to_close_setting")
        if type(value) is int and 0 <= value <= 99 * 60 and value % 60 == 0:
            return value // 60
        return None

    @property
    def available(self) -> bool:
        device = self.device
        return (
            super().available
            and device is not None
            and device.can_move("close")
            and self.native_value is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        if type(value) not in {float, int} or not float(value).is_integer():
            raise translated("whole_minutes")
        await self.coordinator.execute(
            self.coordinator.garage.set_time_to_close(self.device, int(value))
        )

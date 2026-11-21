"""MyQ linked thermostats, using their reported configuration and limits."""

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .api import climate as api
from .coordinator import SETTLE_SECONDS
from .entity import MyQEntity, discover_entities
from .exceptions import translated

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    discover_entities(
        entry.runtime_data,
        async_add_entities,
        lambda device: (
            [MyQClimate(entry.runtime_data, device)]
            if device.is_climate and api.unit(device) is not None
            else []
        ),
    )


class MyQClimate(MyQEntity, ClimateEntity):
    _attr_name = None
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "climate")

    @property
    def available(self) -> bool:
        return super().available and api.unit(self.device) is not None

    @property
    def temperature_unit(self):
        return (
            UnitOfTemperature.CELSIUS
            if api.unit(self.device) == "celsius"
            else UnitOfTemperature.FAHRENHEIT
        )

    @property
    def current_temperature(self):
        return (
            api.number(self.device.state.get(f"current_temperature_{api.unit(self.device)}"))
            if self.device
            else None
        )

    @property
    def current_humidity(self):
        value = api.number(self.device.state.get("relative_humidity")) if self.device else None
        return value if value is not None and 0 <= value <= 100 else None

    @property
    def hvac_mode(self):
        value = api.setting(self.device, "hvac_mode_setting")
        return HVACMode(value) if value in api.MODES else None

    @property
    def hvac_modes(self):
        return (
            [HVACMode(value) for value in api.modes(self.device)]
            if api.can_control(self.device)
            else []
        )

    @property
    def hvac_action(self):
        """HvacDevice.j2: the app infers running from current versus target temperature."""
        mode, temperature = self.hvac_mode, self.current_temperature
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        if temperature is None:
            return None
        heating, cooling = api.target(self.device, "heating"), api.target(self.device, "cooling")
        if mode == HVACMode.HEAT and heating is not None:
            return HVACAction.HEATING if temperature < heating else HVACAction.IDLE
        if mode == HVACMode.COOL and cooling is not None:
            return HVACAction.COOLING if temperature > cooling else HVACAction.IDLE
        if mode == HVACMode.HEAT_COOL and heating is not None and cooling is not None:
            if temperature < heating:
                return HVACAction.HEATING
            return HVACAction.COOLING if temperature > cooling else HVACAction.IDLE
        return None

    @property
    def extra_state_attributes(self):
        band = api.threshold(self.device)
        return {"temperature_alert_band": band} if band is not None else None

    @property
    def supported_features(self):
        features = ClimateEntityFeature(0)
        if not api.can_control(self.device):
            return features
        if api.modes(self.device, fan=True):
            features |= ClimateEntityFeature.FAN_MODE
        if any(
            mode in api.modes(self.device) and api.limits(self.device, kind)
            for mode, kind in (("heat", "heating"), ("cool", "cooling"))
        ):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if "heat_cool" in api.modes(self.device) and all(
            api.limits(self.device, kind) for kind in ("heating", "cooling")
        ):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        return features

    @property
    def fan_mode(self):
        value = api.setting(self.device, "fan_mode_setting")
        return value if value in api.FAN_MODES else None

    @property
    def fan_modes(self):
        return api.modes(self.device, fan=True)

    @property
    def target_temperature(self):
        if self.hvac_mode == HVACMode.HEAT:
            return api.target(self.device, "heating")
        if self.hvac_mode == HVACMode.COOL:
            return api.target(self.device, "cooling")
        return None

    @property
    def target_temperature_low(self):
        return api.target(self.device, "heating") if self.hvac_mode == HVACMode.HEAT_COOL else None

    @property
    def target_temperature_high(self):
        return api.target(self.device, "cooling") if self.hvac_mode == HVACMode.HEAT_COOL else None

    def _bounds(self) -> list[tuple[float, float]]:
        kinds = (
            ("heating",)
            if self.hvac_mode == HVACMode.HEAT
            else ("cooling",)
            if self.hvac_mode == HVACMode.COOL
            else ("heating", "cooling")
        )
        return [bounds for kind in kinds if (bounds := api.limits(self.device, kind)) is not None]

    @property
    def min_temp(self):
        return min((bounds[0] for bounds in self._bounds()), default=0)

    @property
    def max_temp(self):
        return max((bounds[1] for bounds in self._bounds()), default=0)

    @property
    def target_temperature_step(self):
        return 0.1 if api.unit(self.device) == "celsius" else 1

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.execute(
            self.coordinator.thermostats.set_mode(self.device, hvac_mode), settle=SETTLE_SECONDS
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self.coordinator.execute(
            self.coordinator.thermostats.set_fan(self.device, fan_mode), settle=SETTLE_SECONDS
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        heating, cooling = kwargs.get(ATTR_TARGET_TEMP_LOW), kwargs.get(ATTR_TARGET_TEMP_HIGH)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None and (heating is not None or cooling is not None):
            raise translated("temperature_or_range")
        if temperature is None and heating is None and cooling is None:
            raise translated("temperature_required")
        # The mode just sent, not a readback that may still show the previous one.
        mode = kwargs.get(ATTR_HVAC_MODE) or (
            self.coordinator.thermostats.settings(self.device)["mode"] if self.device else None
        )
        if temperature is not None and mode not in (HVACMode.HEAT, HVACMode.COOL):
            raise translated("temperature_needs_mode")
        await self.coordinator.execute(
            self.coordinator.thermostats.set_temperature(
                self.device, temperature=temperature, heating=heating, cooling=cooling, mode=mode
            ),
            settle=SETTLE_SECONDS,
        )

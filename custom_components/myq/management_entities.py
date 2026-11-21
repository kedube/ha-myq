"""Management entities require explicit account grants and observed settings."""

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .api.garage import can_set_lock_mode
from .api.locks import is_lock
from .api.management import (
    AUTO_LOCK_SECONDS,
    AUTO_RELOCK_SECONDS,
    can_auto_lock,
    can_auto_relock,
    can_configure_door_sensor,
    can_reset_pin_lockout,
)
from .entity import MyQEntity
from .exceptions import translated


def _access(coordinator, device):
    return (
        getattr(coordinator.data, "account_access", {}).get(device.account_id) if device else None
    )


def shown_by_control(coordinator, device, key: str) -> bool:
    """Whether a cover, light, lock, switch or select already presents this state field.

    Such diagnostic copies start disabled; they stay available for users who enable them.
    A lock's door_state is its door sensor, which no control entity shows.
    """
    if key == "door_state":
        # A lock's door contact is shown by its Door binary sensor.
        return device.is_door or is_lock(device)
    if key == "in_vacation_mode":
        return can_set_lock_mode(device, _access(coordinator, device))
    if key == "lamp_state":
        return device.is_lamp
    if key == "lock_state":
        return is_lock(device)
    if key in {"auto_relock_time", "auto_relock_enabled"}:
        return can_auto_relock(device, _access(coordinator, device))
    if key == "automatic_lock_state":
        return can_auto_lock(device, _access(coordinator, device))
    if key == "door_sensor_state":
        return can_configure_door_sensor(device, _access(coordinator, device))
    return False


def _alerts_manageable(coordinator, device) -> bool:
    access = _access(coordinator, device)
    return access is not None and access.allows("alerts", "Manage")


def management_switch_entities(coordinator, device):
    if can_set_lock_mode(device, _access(coordinator, device)):
        yield MyQLockMode(coordinator, device, "lock_mode")
    if _alerts_manageable(coordinator, device):
        for rule in getattr(coordinator.data, "alerts", {}).get(device.key, ()):
            yield MyQAlertRule(coordinator, device, rule)
    if can_auto_relock(device, _access(coordinator, device)):
        yield MyQAutoRelock(coordinator, device, "auto_relock")
    if can_configure_door_sensor(device, _access(coordinator, device)):
        yield MyQDoorSensor(coordinator, device, "door_sensor_enabled")


def management_select_entities(coordinator, device):
    if can_auto_relock(device, _access(coordinator, device)):
        yield MyQAutoRelockDelay(coordinator, device, "auto_relock_delay")
    if can_auto_lock(device, _access(coordinator, device)):
        yield MyQAutomaticLockDelay(coordinator, device, "automatic_lock_delay")


def management_button_entities(coordinator, device):
    if can_reset_pin_lockout(device, _access(coordinator, device)):
        yield MyQClearPinLockout(coordinator, device, "clear_pin_lockout")


def management_sensor_entities(coordinator, device):
    for accessory in getattr(coordinator.data, "accessories", {}).get(device.key, ()):
        yield MyQAccessoryBattery(coordinator, device, accessory)


class _AutoRelockSetting(MyQEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self):
        return super().available and can_auto_relock(
            self.device, _access(self.coordinator, self.device)
        )


class MyQAutoRelock(_AutoRelockSetting, SwitchEntity):
    _attr_translation_key = "auto_relock"
    _attr_icon = "mdi:lock-clock"

    @property
    def is_on(self):
        return self.device.state.get("auto_relock_enabled") if self.device else None

    async def _set(self, enabled):
        if not self.available:
            raise translated("setting_unavailable")
        await self.coordinator.execute(
            self.coordinator.management.set_auto_relock(
                self.device, enabled, self.device.state["auto_relock_time"]
            )
        )

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class MyQAutoRelockDelay(_AutoRelockSetting, SelectEntity):
    _attr_translation_key = "auto_relock_delay"
    _attr_icon = "mdi:timer-lock"
    _attr_options = [str(seconds) for seconds in AUTO_RELOCK_SECONDS]

    @property
    def current_option(self):
        seconds = self.device.state.get("auto_relock_time") if self.device else None
        return str(seconds) if seconds in AUTO_RELOCK_SECONDS else None

    async def async_select_option(self, option):
        if option not in self.options or not self.available:
            raise translated("unsupported_option")
        seconds = AUTO_RELOCK_SECONDS[self.options.index(option)]
        await self.coordinator.execute(
            self.coordinator.management.set_auto_relock(
                self.device, self.device.state["auto_relock_enabled"], seconds
            )
        )


class MyQAccessoryBattery(MyQEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_options = ["good", "low", "critical", "unknown"]

    def __init__(self, coordinator, device, accessory):
        super().__init__(coordinator, device, f"accessory_{accessory.serial}_battery")
        self.accessory_serial = accessory.serial
        self._attr_translation_key = "accessory_battery"
        self._attr_translation_placeholders = {"accessory": accessory.name}

    @property
    def native_value(self):
        for accessory in getattr(self.coordinator.data, "accessories", {}).get(self.device_key, ()):
            if accessory.serial == self.accessory_serial:
                return accessory.battery
        return None

    @property
    def available(self):
        return super().available and self.native_value is not None


class MyQAutomaticLockDelay(MyQEntity, SelectEntity):
    _attr_translation_key = "automatic_lock_delay"
    _attr_icon = "mdi:lock-clock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["off", *(str(seconds) for seconds in AUTO_LOCK_SECONDS if seconds)]

    @property
    def available(self):
        return super().available and can_auto_lock(
            self.device, _access(self.coordinator, self.device)
        )

    @property
    def current_option(self):
        if self.device is None:
            return None
        if self.device.state.get("automatic_lock_state") == "disabled":
            return "off"
        seconds = self.device.state.get("automatic_lock_delay_duration")
        return str(seconds) if type(seconds) is int and seconds in AUTO_RELOCK_SECONDS else None

    async def async_select_option(self, option):
        if option not in self.options or not self.available:
            raise translated("unsupported_option")
        await self.coordinator.execute(
            self.coordinator.management.set_auto_lock(
                self.device, AUTO_LOCK_SECONDS[self.options.index(option)]
            )
        )


class MyQDoorSensor(MyQEntity, SwitchEntity):
    _attr_translation_key = "door_sensor"
    _attr_icon = "mdi:door-closed"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self):
        return super().available and can_configure_door_sensor(
            self.device, _access(self.coordinator, self.device)
        )

    @property
    def is_on(self):
        if self.device is None:
            return None
        value = self.device.state.get("door_sensor_state")
        return (
            {"enabled": True, "disabled": False}.get(value.lower())
            if isinstance(value, str)
            else None
        )

    async def _set(self, enabled):
        if not self.available:
            raise translated("setting_unavailable")
        await self.coordinator.execute(
            self.coordinator.management.set_door_sensor(self.device, enabled)
        )

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class MyQLockMode(MyQEntity, SwitchEntity):
    """The app's opener lock mode: the physical remotes and keypads stop working."""

    _attr_translation_key = "lock_mode"
    _attr_icon = "mdi:remote-off"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self):
        return super().available and can_set_lock_mode(
            self.device, _access(self.coordinator, self.device)
        )

    @property
    def is_on(self):
        # iv0/b.g: null reads as false.
        return self.device.state.get("in_vacation_mode") is True if self.device else None

    async def _set(self, enabled):
        if not self.available:
            raise translated("setting_unavailable")
        await self.coordinator.execute(
            self.coordinator.management.set_lock_mode(self.device, enabled)
        )

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class MyQClearPinLockout(MyQEntity, ButtonEntity):
    """Clears a wrong-PIN keypad lockout. Security-sensitive, so disabled by default."""

    _attr_translation_key = "clear_pin_lockout"
    _attr_icon = "mdi:lock-reset"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    @property
    def available(self):
        return (
            super().available
            and can_reset_pin_lockout(self.device, _access(self.coordinator, self.device))
            and self.device.state.get("failed_credential_lockout_active") is True
        )

    async def async_press(self) -> None:
        device = self.device
        if device is None or not self.available:
            raise translated("no_lockout")
        await self.coordinator.execute(self.coordinator.management.reset_pin_lockout(device))


class MyQAlertRule(MyQEntity, SwitchEntity):
    """One MyQ notification rule for this device (AlertDto.is_enabled)."""

    _attr_icon = "mdi:bell-badge"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device, rule):
        super().__init__(coordinator, device, f"alert_{rule.id}")
        self.rule_id = rule.id
        self._attr_translation_key = "alert_rule"
        self._attr_translation_placeholders = {"rule": rule.name[:72]}

    @property
    def rule(self):
        for rule in getattr(self.coordinator.data, "alerts", {}).get(self.device_key, ()):
            if rule.id == self.rule_id:
                return rule
        return None

    @property
    def available(self):
        return (
            super().available
            and self.rule is not None
            and _alerts_manageable(self.coordinator, self.device)
        )

    @property
    def is_on(self):
        rule = self.rule
        return rule.enabled if rule is not None else None

    @property
    def extra_state_attributes(self):
        rule = self.rule
        return {"events": rule.summary()["events"]} if rule is not None else None

    async def _set(self, enabled):
        if not self.available:
            raise translated("setting_unavailable")
        await self.coordinator.execute(
            self.coordinator.async_set_alert(self.device, self.rule_id, enabled)
        )

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)

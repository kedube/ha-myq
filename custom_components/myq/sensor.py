"""Expose only state fields returned by a registered device."""

from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.helpers import device_registry as dr

from .api.associations import associations
from .api.locks import is_lock
from .camera_entities import camera_entities
from .device_registry import find_device
from .entity import MyQEntity, discover_entities, shared_with_opener
from .management_entities import management_sensor_entities, shown_by_control

PARALLEL_UPDATES = 0


@dataclass(frozen=True)
class Field:
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    options: tuple[str, ...] | None = None
    # Last reported values that stay meaningful while the device is offline.
    offline: bool = False
    enabled: bool = True
    category: EntityCategory | None = EntityCategory.DIAGNOSTIC


# BatteryBackupState (xt/a.java) and the lock orientation values (LockDevice.F0).
BACKUP_BATTERY = ("idle", "charging", "bad", "disconnected", "none", "unknown")
ORIENTATIONS = ("lefthand", "righthand", "unknown", "calibrationfailure")
WHOLE = {
    "battery_percentage",
    "absolute_cycle_count",
    "auto_relock_time",
    "failed_credential_lockout_frequency",
    "failed_credential_lockout_duration_seconds",
}
FIELDS = {
    "door_state": Field(),
    "battery_backup_state": Field(SensorDeviceClass.ENUM, options=BACKUP_BATTERY),
    "absolute_cycle_count": Field(state_class=SensorStateClass.TOTAL_INCREASING, offline=True),
    "wifi_signal_strength": Field(),
    "battery_percentage": Field(
        SensorDeviceClass.BATTERY,
        PERCENTAGE,
        SensorStateClass.MEASUREMENT,
        offline=True,
    ),
    "lamp_state": Field(),
    "offline_reason": Field(offline=True),
    "lock_state": Field(),
    "bluetooth_connectability": Field(),
    "auto_relock_time": Field(SensorDeviceClass.DURATION, UnitOfTime.SECONDS),
    "automatic_lock_state": Field(),
    "door_sensor_state": Field(),
    "door_sensor_calibration_status": Field(),
    "orientation": Field(SensorDeviceClass.ENUM, options=ORIENTATIONS),
    "failed_credential_lockout_frequency": Field(
        state_class=SensorStateClass.MEASUREMENT, enabled=False
    ),
    "failed_credential_lockout_duration_seconds": Field(
        SensorDeviceClass.DURATION, UnitOfTime.SECONDS, enabled=False
    ),
}
LAST_CHANGE = Field(SensorDeviceClass.TIMESTAMP, offline=True, category=None)


def parse_timestamp(value) -> datetime | None:
    """ct0/a: SimpleDateFormat yyyy-MM-dd'T'HH:mm:ss in UTC, parsing a leading prefix.

    Like the app, anything after whole seconds (fractions, Z, an offset) is ignored.
    """
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    def factory(device):
        coordinator = entry.runtime_data
        if not shared_with_opener(coordinator, device, ("sensor", "device_type")):
            yield MyQDeviceType(coordinator, device)
        for key in FIELDS:
            if key in device.state:
                yield MyQSensor(coordinator, device, key)
        if (
            "last_update" in device.state or "last_status" in device.state
        ) and not shared_with_opener(coordinator, device, ("sensor", "last_update")):
            yield MyQLastChange(coordinator, device)
        yield from camera_entities(coordinator, device, "sensor")
        yield from management_sensor_entities(coordinator, device)

    discover_entities(entry.runtime_data, async_add_entities, factory)


class MyQDeviceType(MyQEntity, SensorEntity):
    """Keep gateway/accessory/unknown records visible without inventing commands."""

    _attr_translation_key = "device_type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "device_type")

    @property
    def available(self):
        return self.coordinator.last_update_success and self.device is not None

    @property
    def native_value(self):
        return self.device.device_type if self.device else None

    @property
    def extra_state_attributes(self):
        if self.device is None:
            return None
        access = self.coordinator.data.account_access.get(self.device.account_id)
        attributes = {"account_role": access.role if access is not None else "unknown"}
        registry = dr.async_get(self.hass)
        entry_id = self.coordinator.config_entry.entry_id
        own_entry = find_device(registry, entry_id, self.device.key)
        for kind, devices in associations(self.device, self.coordinator.data.devices).items():
            attributes[kind] = [
                entry.id
                for device in devices
                if (entry := find_device(registry, entry_id, device.key)) is not None
                and (own_entry is None or entry.id != own_entry.id)
            ]
        return attributes


class _StateSensor(MyQEntity, SensorEntity):
    field: Field

    def _apply(self, field: Field, key: str) -> None:
        self.field = field
        self._attr_translation_key = key
        self._attr_device_class = field.device_class
        self._attr_native_unit_of_measurement = field.unit
        self._attr_state_class = field.state_class
        self._attr_entity_category = field.category
        if field.options is not None:
            self._attr_options = list(field.options)

    @property
    def available(self) -> bool:
        if self.field.offline:
            return self.coordinator.last_update_success and self.device is not None
        return super().available


class MyQSensor(_StateSensor):
    def __init__(self, coordinator, device, key) -> None:
        super().__init__(coordinator, device, key)
        self.key = key
        self._apply(FIELDS[key], key)
        self._attr_entity_registry_enabled_default = FIELDS[key].enabled and not (
            shown_by_control(coordinator, device, key)
        )

    @property
    def native_value(self):
        value = self.device.state.get(self.key) if self.device else None
        if self.key == "door_state" and self.device and is_lock(self.device):
            enabled = self.device.state.get("door_sensor_state")
            if not isinstance(enabled, str) or enabled.lower() != "enabled":
                return None
            return (
                value.lower()
                if isinstance(value, str) and value.lower() in {"open", "closed"}
                else None
            )
        if self.field.options is not None:
            value = value.lower() if isinstance(value, str) else None
            return value if value in self.field.options else None
        if self.key in WHOLE:
            if isinstance(value, bool):
                return None
            try:
                value = int(value)
            except (ValueError, TypeError, OverflowError):
                return None
            if value < 0 or (self.key == "battery_percentage" and value > 100):
                return None
        return value if isinstance(value, (str, int, float)) else None


class MyQLastChange(_StateSensor):
    """The door tile's "state · time ago" (Device.D0 last_update, E0 last_status)."""

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "last_update")
        self._apply(LAST_CHANGE, "last_update")

    @property
    def native_value(self) -> datetime | None:
        if self.device is None:
            return None
        # Device.D0: last_status while offline and for Wi-Fi lock gateways.
        offline = not self.device.online or self.device.device_type == "wifilockgateway"
        return parse_timestamp(self.device.state.get("last_status" if offline else "last_update"))

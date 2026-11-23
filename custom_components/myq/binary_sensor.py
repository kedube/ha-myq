"""Connectivity, monitoring mode, and diagnostic flags."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .api.locks import is_lock
from .api.models import Device
from .camera_entities import camera_entities
from .entity import MyQEntity, built_in_gateways, discover_entities, shared_with_opener
from .management_entities import shown_by_control

PARALLEL_UPDATES = 0


def _flag(key: str) -> Callable[[Device], bool | None]:
    def read(device: Device) -> bool | None:
        value = device.state.get(key)
        return value if type(value) is bool else None

    return read


def _sensor_fault(device: Device) -> bool | None:
    # GdoDeviceUiItem: a communication fault counts only while a sensor is learned.
    value = device.state.get("sensor_comm_error")
    learned = device.state.get("number_of_learned_dps_devices")
    if type(value) is not bool:
        return None
    return value and not (type(learned) is int and learned == 0)


def faults(device: Device) -> list[dict[str, Any]] | None:
    """active_fault_codes[] {code, description, activated_timestamp} (Device.A0)."""
    raw = device.state.get("active_fault_codes")
    if not isinstance(raw, list):
        return None
    result = []
    for item in raw[:32]:
        if isinstance(item, dict):
            result.append(
                {
                    key: item[key]
                    for key in ("code", "description", "activated_timestamp")
                    if isinstance(item.get(key), (str, int)) and not isinstance(item[key], bool)
                }
            )
    return result


def _has_faults(device: Device) -> bool | None:
    found = faults(device)
    return None if found is None else bool(found)


def _backup_problem(device: Device) -> bool | None:
    value = device.state.get("battery_backup_state")
    if not isinstance(value, str):
        return None
    value = value.lower()
    if value in {"bad", "disconnected"}:
        return True
    return False if value in {"idle", "charging", "none"} else None


def _misaligned(device: Device) -> bool | None:
    # The opener health screen (xt/k1) reads this flag as safety sensor alignment.
    value = device.state.get("is_unattended_close_allowed")
    return (not value) if type(value) is bool else None


def _calibration_failed(device: Device) -> bool | None:
    value = device.state.get("orientation")
    return value.lower() == "calibrationfailure" if isinstance(value, str) else None


def _door_open(device: Device) -> bool | None:
    # The app's door-ajar banner reads the lock door contact only while enabled.
    enabled, value = device.state.get("door_sensor_state"), device.state.get("door_state")
    if not isinstance(enabled, str) or enabled.lower() != "enabled" or not isinstance(value, str):
        return None
    return {"open": True, "closed": False}.get(value.lower())


@dataclass(frozen=True)
class Flag:
    device_class: BinarySensorDeviceClass | None = None
    read: Callable[[Device], bool | None] | None = None
    applies: Callable[[Device], bool] = lambda device: True
    offline: bool = False
    enabled: bool = True
    category: EntityCategory | None = EntityCategory.DIAGNOSTIC


PROBLEM = BinarySensorDeviceClass.PROBLEM
OPENERS = {"garagedooropener", "wifigaragedooropener"}
FIELDS: dict[str, Flag] = {
    # Effective connectivity, including the Wi-Fi lock gateway's camera rule.
    "online": Flag(BinarySensorDeviceClass.CONNECTIVITY, lambda d: d.online, offline=True),
    "sensor_comm_error": Flag(PROBLEM, _sensor_fault),
    "dps_low_battery_mode": Flag(BinarySensorDeviceClass.BATTERY, offline=True),
    "monitor_only_mode": Flag(),
    "is_unattended_open_allowed": Flag(),
    "is_unattended_close_allowed": Flag(),
    # Lock mode in the app (c2.java:224 lockModelEnabled): remotes and keypads disabled.
    "in_vacation_mode": Flag(),
    "camera_online": Flag(BinarySensorDeviceClass.CONNECTIVITY),
    "low_battery": Flag(BinarySensorDeviceClass.BATTERY, offline=True),
    "auto_relock_enabled": Flag(),
    "allow_auto_relock": Flag(),
    "is_managed": Flag(),
    "attached_work_light_error_present": Flag(PROBLEM),
    "failed_credential_lockout_active": Flag(PROBLEM, category=None),
    "failed_credential_lockout_enabled": Flag(enabled=False),
}
# Derived from another field, so their unique IDs use their own suffix.
DERIVED: dict[str, tuple[str, Flag]] = {
    "faults": (
        "active_fault_codes",
        Flag(PROBLEM, _has_faults, offline=True),
    ),
    "backup_battery_problem": (
        "battery_backup_state",
        Flag(PROBLEM, _backup_problem),
    ),
    "safety_sensors": (
        "is_unattended_close_allowed",
        Flag(
            PROBLEM,
            _misaligned,
            applies=lambda d: d.device_type in OPENERS,
        ),
    ),
    "lock_calibration": (
        "orientation",
        Flag(PROBLEM, _calibration_failed, applies=is_lock),
    ),
    "lock_door": (
        "door_sensor_state",
        Flag(BinarySensorDeviceClass.DOOR, _door_open, applies=is_lock, category=None),
    ),
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    def factory(device):
        coordinator = entry.runtime_data
        for key, flag in FIELDS.items():
            if key == "online":
                # One Connectivity entity for an opener and its built-in gateway.
                parts = [device, *built_in_gateways(coordinator, device)]
                if any(key in part.state for part in parts) and not shared_with_opener(
                    coordinator, device, ("binary_sensor", key)
                ):
                    yield MyQConnectivity(coordinator, device)
            elif key in device.state and flag.applies(device):
                yield MyQBinarySensor(coordinator, device, key, key, flag)
        for suffix, (key, flag) in DERIVED.items():
            if key in device.state and flag.applies(device):
                yield MyQBinarySensor(coordinator, device, suffix, key, flag)
        yield from camera_entities(coordinator, device, "binary_sensor")

    discover_entities(entry.runtime_data, async_add_entities, factory)


class MyQBinarySensor(MyQEntity, BinarySensorEntity):
    def __init__(self, coordinator, device, suffix: str, key: str, flag: Flag) -> None:
        super().__init__(coordinator, device, suffix)
        self.key, self.flag = key, flag
        self._read = flag.read or _flag(key)
        self._attr_translation_key, self._attr_device_class = suffix, flag.device_class
        self._attr_entity_category = flag.category
        self._attr_entity_registry_enabled_default = flag.enabled and not shown_by_control(
            coordinator, device, suffix
        )

    @property
    def available(self) -> bool:
        if self.flag.offline:
            return self.coordinator.last_update_success and self.device is not None
        return super().available

    @property
    def is_on(self) -> bool | None:
        return self._read(self.device) if self.device else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.key == "active_fault_codes" and self.device is not None:
            return {"faults": faults(self.device) or []}
        return None


class MyQConnectivity(MyQBinarySensor):
    """Connected only while the device and any Wi-Fi gateway built into it are online."""

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "online", "online", FIELDS["online"])

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None:
            return None
        return device.online and all(
            gateway.online for gateway in built_in_gateways(self.coordinator, device)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        gateways = built_in_gateways(self.coordinator, device) if device is not None else []
        if device is None or not gateways:
            return None
        # Which half of a merged opener is offline.
        return {
            "opener_online": device.online,
            "gateway_online": all(gateway.online for gateway in gateways),
        }

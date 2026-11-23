"""Read-only firmware status. Installing firmware is deliberately not offered."""

from typing import Any

from homeassistant.components.update import UpdateEntity
from homeassistant.const import EntityCategory

from .api.models import Device, nested
from .entity import MyQEntity, discover_entities

PARALLEL_UPDATES = 0


def _version(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= 64 else None


def camera_firmware(details: dict) -> tuple[str, str, bool | None] | None:
    """Tend hw.firmwareVer, hw.firmwareUpgradeAvail (xr0/FirmwareSettings), upgradeInProgress."""
    installed = _version(nested(details, "hw", "firmwareVer"))
    if installed is None:
        return None
    available = _version(nested(details, "hw", "firmwareUpgradeAvail"))
    progress = nested(details, "status", "upgradeInProgress")
    return installed, available or installed, progress if type(progress) is bool else None


def device_firmware(device: Device) -> tuple[str, str | None] | None:
    """MyQ firmware_version, with latest_available_firmware_version when firmware_available."""
    installed = _version(device.state.get("firmware_version"))
    flag = device.state.get("firmware_available")
    if installed is None or type(flag) is not bool:
        return None
    if not flag:
        return installed, installed
    return installed, _version(device.state.get("latest_available_firmware_version"))


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    def factory(device):
        coordinator = entry.runtime_data
        if device.is_camera:
            if camera_firmware(coordinator.data.cameras.get(device.key, {})) is not None:
                yield MyQFirmware(coordinator, device)
        elif device_firmware(device) is not None:
            yield MyQFirmware(coordinator, device)

    discover_entities(entry.runtime_data, async_add_entities, factory)


class MyQFirmware(MyQEntity, UpdateEntity):
    _attr_translation_key = "firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "firmware")

    def _read(self) -> tuple[str | None, str | None, bool | None]:
        device = self.device
        if device is None:
            return None, None, None
        if device.is_camera:
            camera = camera_firmware(self.camera_details)
            return camera if camera is not None else (None, None, None)
        reported = device_firmware(device)
        return (*reported, None) if reported is not None else (None, None, None)

    @property
    def entity_picture(self) -> str | None:
        # Cameras show the update icons (mdi:package, mdi:package-up) instead of the brand.
        device = self.device
        if device is not None and device.is_camera:
            return None
        return super().entity_picture

    @property
    def available(self) -> bool:
        return super().available and self._read()[0] is not None

    @property
    def installed_version(self) -> str | None:
        return self._read()[0]

    @property
    def latest_version(self) -> str | None:
        return self._read()[1]

    @property
    def in_progress(self) -> bool | None:
        return self._read()[2]

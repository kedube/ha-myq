"""Device identity, availability, and dynamic entity discovery."""

import re
from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import Device, nested
from .const import DOMAIN
from .coordinator import MyQCoordinator
from .device_registry import GATEWAY_SHARED, find_device


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= 128 else None


def shared_with_opener(coordinator: MyQCoordinator, device: Device, role: tuple[str, str]) -> bool:
    """A merged Wi-Fi gateway's (platform, suffix) role that its opener's entity covers."""
    return role in GATEWAY_SHARED and device.key in getattr(coordinator, "gateway_openers", {})


def built_in_gateways(coordinator: MyQCoordinator, device: Device) -> list[Device]:
    """The Wi-Fi gateways merged into this opener's device."""
    devices = coordinator.data.devices
    return [
        devices[key]
        for key, opener in getattr(coordinator, "gateway_openers", {}).items()
        if opener == device.key and key in devices
    ]


def device_info(coordinator: MyQCoordinator, device: Device) -> DeviceInfo:
    groups = getattr(coordinator, "gateway_openers", {})
    if device.key in groups:
        device = coordinator.data.devices[groups[device.key]]
    elif device.device_type == "wifigdogateway":
        registered = find_device(
            dr.async_get(coordinator.hass), coordinator.config_entry.entry_id, device.key
        )
        if registered is not None and any(
            domain == DOMAIN and key != device.key for domain, key in registered.identifiers
        ):
            # A previously merged opener may be absent from a partial inventory.
            # Retain its name/model rather than overwriting them with the gateway.
            return DeviceInfo(identifiers={(DOMAIN, device.key)})
    details = getattr(coordinator.data, "cameras", {}).get(device.key, {})
    firmware = device.state.get("firmware_version") or next(
        (
            coordinator.data.devices[key].state.get("firmware_version")
            for key, opener in groups.items()
            if opener == device.key
        ),
        None,
    )
    info = DeviceInfo(
        identifiers={(DOMAIN, device.key)}
        | {(DOMAIN, key) for key, opener in groups.items() if opener == device.key},
        name=device.name,
        manufacturer=device.state.get("brand_name") or "Chamberlain Group",
        model=device.model or _text(device.state.get("lock_model")) or device.device_type,
        # The camera settings "F/W" row reads the Tend hardware version.
        sw_version=_text(nested(details, "hw", "firmwareVer")) or firmware,
    )
    mac = _text(nested(details, "net", "eth", "mac"))
    if mac is not None and re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}", mac):
        info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}
    return info


class MyQEntity(CoordinatorEntity[MyQCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MyQCoordinator, device: Device, suffix: str) -> None:
        super().__init__(coordinator)
        self.device_key = device.key
        self._attr_unique_id = f"{device.key}:{suffix}"
        self._attr_device_info = device_info(coordinator, device)

    @property
    def device(self) -> Device | None:
        return self.coordinator.data.devices.get(self.device_key)

    @property
    def camera_details(self) -> dict[str, Any]:
        """Latest Tend details for this device; empty when not a camera or not read."""
        return self.coordinator.data.cameras.get(self.device_key, {})

    @property
    def account_owner(self) -> bool:
        """Device settings screens are the account owner's in the app (y80/k, y80/x)."""
        device = self.device
        access = self.coordinator.data.account_access.get(device.account_id) if device else None
        return access is not None and access.owner

    @property
    def available(self) -> bool:
        return super().available and self.device is not None and self.device.online


@callback
def discover_entities(
    coordinator: MyQCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[Device], Iterable[MyQEntity]],
) -> None:
    domain = async_get_current_platform().domain
    registry = er.async_get(coordinator.hass)
    known: set[str] = set()

    @callback
    def discover() -> None:
        new = []
        for device in coordinator.data.devices.values():
            for entity in factory(device):
                unique_id = entity.unique_id
                if unique_id is None:
                    continue
                # Offered before and still registered: leave it alone, whether it is
                # loaded, disabled or was given another entity ID. Home Assistant runs
                # an entity's removal callbacks in all of those cases, so they cannot
                # tell when to offer it again. A pruned device loses its registry
                # entries, so its entities are offered again if it returns.
                if unique_id in known and registry.async_get_entity_id(domain, DOMAIN, unique_id):
                    continue
                known.add(unique_id)
                new.append(entity)
        if new:
            async_add_entities(new)

    discover()
    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(discover))

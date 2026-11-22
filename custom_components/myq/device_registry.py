"""Join an opener and its embedded Wi-Fi gateway without recreating entities."""

from collections.abc import Iterable
from typing import cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .api.models import Device
from .const import DOMAIN


def _find_device(registry: dr.DeviceRegistry, entry_id: str, key: str) -> dr.DeviceEntry | None:
    # HA 2026.11 scopes identifiers to their config entry; older versions don't.
    if finder := getattr(registry, "async_get_device_by_identifier", None):
        return finder((DOMAIN, key), entry_id)
    return registry.async_get_device(identifiers={(DOMAIN, key)})


def _owned_device(
    device: dr.DeviceEntry,
    entities: er.EntityRegistry,
    entry_id: str,
    identifiers: set[tuple[str, str]],
) -> bool:
    """Do not absorb another integration, account, or independently registered device."""
    if getattr(device, "is_composite_device", False):
        return False
    owner = getattr(device, "config_entry_id", None)
    owners = {owner} if owner is not None else device.config_entries
    if owners != {entry_id}:
        return False
    return device.identifiers <= identifiers and all(
        entity.platform == DOMAIN and entity.config_entry_id == entry_id
        for entity in er.async_entries_for_device(
            entities, device.id, include_disabled_entities=True
        )
    )


@callback
def async_merge_opener_gateway(
    hass: HomeAssistant, entry: ConfigEntry, opener: Device, gateway: Device
) -> bool:
    """Register a confirmed pair together, retaining existing entity and opener IDs.

    The caller must establish an unambiguous physical relationship from inventory.
    All registry changes are synchronous so removal callbacks see the moved entities.
    """
    if (
        opener.device_type != "wifigaragedooropener"
        or gateway.device_type != "wifigdogateway"
        or opener.account_id != gateway.account_id
        or opener.key == gateway.key
    ):
        return False
    devices, entities = dr.async_get(hass), er.async_get(hass)
    identifiers = {(DOMAIN, opener.key), (DOMAIN, gateway.key)}
    opener_entry = _find_device(devices, entry.entry_id, opener.key)
    gateway_entry = _find_device(devices, entry.entry_id, gateway.key)
    if any(
        not _owned_device(device, entities, entry.entry_id, identifiers)
        for device in (opener_entry, gateway_entry)
        if device is not None
    ):
        return False

    canonical = opener_entry or gateway_entry
    if canonical is None:
        devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers=identifiers,
            name=opener.name,
            manufacturer=opener.state.get("brand_name") or "Chamberlain Group",
            model=opener.model or opener.device_type,
            sw_version=opener.state.get("firmware_version")
            or gateway.state.get("firmware_version"),
        )
        return True

    connections = canonical.connections
    labels = canonical.labels
    area = canonical.area_id
    user_name = canonical.name_by_user
    via_device_id = canonical.via_device_id
    if gateway_entry is not None and gateway_entry.id != canonical.id:
        connections = connections | gateway_entry.connections
        labels = labels | gateway_entry.labels
        area = area or gateway_entry.area_id
        user_name = user_name or gateway_entry.name_by_user
        for entity in er.async_entries_for_device(
            entities, gateway_entry.id, include_disabled_entities=True
        ):
            # A device-disabled entity must remain disabled after moving to an
            # enabled opener; HA 2026.11 otherwise recalculates this inherited flag.
            if entity.disabled_by is er.RegistryEntryDisabler.DEVICE and not canonical.disabled:
                entities.async_update_entity(
                    entity.entity_id,
                    device_id=canonical.id,
                    disabled_by=er.RegistryEntryDisabler.USER,
                )
            else:
                entities.async_update_entity(entity.entity_id, device_id=canonical.id)
        # HA 2026.11 changed this public collection from a mapping to an iterable.
        registered_devices = (
            list(cast(Iterable[dr.DeviceEntry], devices.devices))
            if hasattr(devices, "async_get_device_by_identifier")
            else list(devices.devices.values())
        )
        for child in registered_devices:
            if child.via_device_id == gateway_entry.id:
                devices.async_update_device(
                    child.id, via_device_id=None if child.id == canonical.id else canonical.id
                )
        if via_device_id == gateway_entry.id:
            via_device_id = None
        # Remove only after migration: HA removes entities still attached here.
        devices.async_remove_device(gateway_entry.id)

    devices.async_update_device(
        canonical.id,
        new_identifiers=canonical.identifiers | identifiers,
        new_connections=connections,
        name=opener.name,
        manufacturer=opener.state.get("brand_name") or "Chamberlain Group",
        model=opener.model or opener.device_type,
        sw_version=opener.state.get("firmware_version") or gateway.state.get("firmware_version"),
        area_id=area,
        name_by_user=user_name,
        labels=labels,
        via_device_id=None if via_device_id == canonical.id else via_device_id,
    )
    return True

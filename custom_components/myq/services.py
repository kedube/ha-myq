"""Typed administrative actions scoped to currently registered MyQ devices."""

import json
import os
import stat
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from .api.climate import programs as thermostat_programs
from .api.errors import CommandRejected, MyQError
from .api.management import AUTO_RELOCK_SECONDS, SCHEDULE_FEATURES, WEEKDAYS
from .api.validation import validate_session
from .const import DOMAIN
from .device_registry import find_device
from .exceptions import translated

_DEVICE: dict[vol.Marker, Any] = {vol.Required("device_id"): cv.string}


def _seconds(choices: tuple[int, ...]) -> Callable[[Any], int]:
    """Whole seconds from YAML or a UI dropdown ("30"); rejects bools and fractions."""

    def validate(value: Any) -> int:
        if isinstance(value, str) and value.isascii() and value.isdigit():
            value = int(value)
        if type(value) is not int or value not in choices:
            raise vol.Invalid(f"Choose one of {', '.join(map(str, choices))} seconds")
        return value

    return validate


_SCHEDULE: dict[vol.Marker, Any] = {
    **_DEVICE,
    vol.Required("feature"): vol.In(SCHEDULE_FEATURES),
    vol.Required("configuration_id"): cv.string,
}
SCHEMAS: dict[str, dict[vol.Marker, Any]] = {
    "validate_session": {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("refresh", default=False): cv.boolean,
    },
    "get_accessories": _DEVICE,
    "rename_accessory": {
        **_DEVICE,
        vol.Required("accessory_serial"): cv.string,
        vol.Required("name"): cv.string,
    },
    "remove_accessory": {**_DEVICE, vol.Required("accessory_serial"): cv.string},
    "set_auto_relock": {
        **_DEVICE,
        vol.Required("enabled"): cv.boolean,
        vol.Required("seconds"): _seconds(AUTO_RELOCK_SECONDS),
    },
    "get_schedules": {**_DEVICE, vol.Required("feature"): vol.In(SCHEDULE_FEATURES)},
    "set_schedule_enabled": {
        **_SCHEDULE,
        vol.Required("schedule_id"): cv.string,
        vol.Required("enabled"): cv.boolean,
    },
    "save_schedule": {
        **_DEVICE,
        vol.Required("feature"): vol.In(SCHEDULE_FEATURES),
        vol.Optional("configuration_id"): cv.string,
        vol.Optional("schedule_id"): cv.string,
        vol.Required("label"): cv.string,
        vol.Required("days"): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)]),
        vol.Required("start_time"): cv.string,
        vol.Required("end_time"): cv.string,
        vol.Required("enabled"): cv.boolean,
    },
    "delete_schedule": {**_SCHEDULE, vol.Required("schedule_id"): cv.string},
    "get_pins": _DEVICE,
    "save_pin": {
        **_DEVICE,
        vol.Required("name"): cv.string,
        vol.Required("pin_file"): cv.string,
        vol.Optional("pin_id"): cv.string,
    },
    "delete_pin": {**_DEVICE, vol.Required("pin_id"): cv.string},
    "set_auto_lock": {
        **_DEVICE,
        vol.Required("seconds"): _seconds((0, *AUTO_RELOCK_SECONDS)),
    },
    "set_door_sensor": {**_DEVICE, vol.Required("enabled"): cv.boolean},
    "rename_device": {**_DEVICE, vol.Required("name"): cv.string},
    "get_device_history": {**_DEVICE, vol.Optional("continuation"): cv.string},
    "get_alerts": _DEVICE,
    "snooze_alerts": {
        **_DEVICE,
        vol.Required("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=7 * 24 * 60)),
    },
    "cancel_alert_snooze": _DEVICE,
    "get_thermostat_programs": _DEVICE,
    "get_guests": _DEVICE,
    "set_guest_devices": {
        **_DEVICE,
        vol.Required("account_user_id"): cv.string,
        vol.Required("access_schedule_id"): cv.string,
        vol.Required("allowed_serials"): vol.All(cv.ensure_list, [cv.string]),
    },
    "remove_guest": {**_DEVICE, vol.Required("account_user_id"): cv.string},
    "assign_accessory": {
        **_DEVICE,
        vol.Required("accessory_serial"): cv.string,
        vol.Required("account_user_id"): cv.string,
    },
}
_RESPONSES = {
    "validate_session",
    "get_accessories",
    "get_schedules",
    "get_pins",
    "get_guests",
    "get_device_history",
    "get_alerts",
    "get_thermostat_programs",
}


def _read_pin(config_dir: str, filename: str) -> str:
    """Keep PIN values out of HA service events, automation traces and responses."""
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise CommandRejected(
            "PIN file must be a filename directly in the HA configuration directory"
        )
    try:
        descriptor = os.open(
            Path(config_dir) / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        try:
            handle = os.fdopen(descriptor, "r", encoding="utf-8")
        except BaseException:
            os.close(descriptor)  # fdopen refuses a directory without closing it.
            raise
        with handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
                or info.st_size > 4096
            ):
                raise ValueError
            value = json.loads(handle.read(4097))
            pin = value.get("pin") if isinstance(value, dict) else None
            if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit():
                raise ValueError
            return pin
    except (OSError, ValueError, UnicodeError):
        raise CommandRejected(
            "PIN file must be an owned private regular JSON file containing a numeric pin string"
        ) from None


def _loaded_entry(hass: HomeAssistant, entry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        raise translated("entry_not_loaded")
    return entry


def _device_target(hass: HomeAssistant, device_id: str):
    registered = dr.async_get(hass).async_get(device_id)
    candidates = {}
    if registered is not None:
        for entry_id in registered.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if (
                entry is None
                or entry.domain != DOMAIN
                or entry.state is not ConfigEntryState.LOADED
            ):
                continue
            for domain, key in registered.identifiers:
                if (
                    domain == DOMAIN
                    and (device := entry.runtime_data.data.devices.get(key)) is not None
                ):
                    coordinator = entry.runtime_data
                    key = coordinator.gateway_openers.get(device.key, device.key)
                    target = coordinator.data.devices.get(key)
                    if target is not None:
                        candidates[(entry_id, key)] = (coordinator, target)
    if len(candidates) != 1:
        raise translated("device_not_found")
    return next(iter(candidates.values()))


@callback
def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "validate_session"):
        return

    async def handle(call: ServiceCall):
        data = dict(call.data)
        if call.service == "validate_session":
            entry = _loaded_entry(hass, data["config_entry_id"])
            return await entry.runtime_data.execute(
                validate_session(
                    entry.runtime_data.client,
                    expected_user_id=entry.unique_id,
                    refresh=data["refresh"],
                )
            )
        coordinator, device = _device_target(hass, data.pop("device_id"))
        management = coordinator.management

        async def operation():
            if call.service == "get_accessories":
                items = await coordinator.async_refresh_accessories(device)
                return {"accessories": [asdict(item) for item in items]}
            if call.service == "get_schedules":
                await management.require_access(device.account_id, "schedules", "View")
                schedules = await management.schedules(device, **data)
                return {
                    "configuration_ids": management.configuration_ids(device, data["feature"]),
                    "schedules": [asdict(item) for item in schedules],
                }
            if call.service == "get_pins":
                return {"pins": [asdict(item) for item in await management.pins(device)]}
            if call.service == "get_guests":
                return {"guests": [asdict(item) for item in await management.guests(device)]}
            if call.service == "get_device_history":
                await management.require_access(device.account_id, "history", "View")
                page = await coordinator.history.read(
                    device.account_id, [device.serial], data.get("continuation")
                )
                events = [
                    event for event in page.events if event.device_serial in (None, device.serial)
                ]
                try:
                    # The opener camera's door-triggered clips, as the app's history shows.
                    clips = await coordinator.door_clip_links(device, events)
                except MyQError:
                    clips = {}
                return {
                    "events": [
                        {**event.as_dict(), "recording": clips.get(event.id)} for event in events
                    ],
                    "continuation": page.continuation,
                }
            if call.service == "get_alerts":
                await management.require_access(device.account_id, "alerts", "View")
                rules = await coordinator.alerts.rules(device.account_id)
                return {
                    "alerts": [rule.summary() for rule in rules if rule.serial == device.serial]
                }
            if call.service == "get_thermostat_programs":
                await management.require_access(device.account_id, "devices", "View")
                return await thermostat_programs(coordinator.client, device)
            if call.service in {"snooze_alerts", "cancel_alert_snooze"}:
                # Account-wide: MyQ snoozes every notification for the account. The app
                # shows the snooze control to owners and co-owners (main/i5).
                await management.require_access(
                    device.account_id, "alerts", "Manage", owner=True, co_owner=True
                )
                if call.service == "snooze_alerts":
                    await coordinator.alerts.snooze(device, data["minutes"])
                else:
                    await coordinator.alerts.cancel_snooze(device)
                return None
            if call.service == "save_schedule" and "configuration_id" not in data:
                if "schedule_id" in data:
                    raise CommandRejected("An existing schedule requires its configuration ID")
                await management.create_schedule(device, **data)
                return None
            if call.service == "save_pin":
                # Only the filename enters HA's service bus. The PIN is not a state,
                # service argument, persisted option, response, or log message.
                pin = await hass.async_add_executor_job(
                    _read_pin, hass.config.config_dir, data.pop("pin_file")
                )
                try:
                    await management.save_pin(device, pin=pin, **data)
                finally:
                    del pin
                return None
            method = {
                "rename_accessory": management.rename_accessory,
                "remove_accessory": management.remove_accessory,
                "set_auto_relock": management.set_auto_relock,
                "set_auto_lock": management.set_auto_lock,
                "set_door_sensor": management.set_door_sensor,
                "rename_device": management.rename_device,
                "set_schedule_enabled": management.set_schedule_enabled,
                "save_schedule": management.save_schedule,
                "delete_schedule": management.delete_schedule,
                "delete_pin": management.delete_pin,
                "set_guest_devices": management.set_guest_devices,
                "remove_guest": management.remove_guest,
                "assign_accessory": management.assign_accessory,
            }[call.service]
            await method(device, **data)
            if call.service in {"rename_accessory", "remove_accessory", "assign_accessory"}:
                coordinator.invalidate_accessories(device)
            if call.service == "rename_device":
                # Entities set the registry name only when added; follow the confirmed name.
                registry = dr.async_get(hass)
                if entry := find_device(registry, coordinator.config_entry.entry_id, device.key):
                    registry.async_update_device(entry.id, name=data["name"].strip())
            return None

        # A read changes nothing: it must not drop every cache and force a full poll.
        runner = coordinator.query if call.service in _RESPONSES else coordinator.execute
        return await runner(operation())

    for name, schema in SCHEMAS.items():
        async_register_admin_service(
            hass,
            DOMAIN,
            name,
            handle,
            vol.Schema(schema),
            supports_response=SupportsResponse.ONLY
            if name in _RESPONSES
            else SupportsResponse.NONE,
        )

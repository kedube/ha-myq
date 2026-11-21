"""Typed management routes recovered from the Android app.

Every write rechecks account grants and inventory, submits once, and confirms
reported state. Management visibility never follows from control permission.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeGuard
from urllib.parse import quote

from .account_access import AccountAccess, owned_access, parse_account_access
from .client import Client
from .const import (
    ACCOUNTS,
    ACTIONS,
    CREDENTIALS,
    DEVICES,
    GATEWAYS,
    GDO,
    LAMPS,
    LOCKS,
    NATIVE_HEADERS,
)
from .errors import CannotConnect, CommandRejected, CommandUncertain, MyQError, ProtocolError
from .exclusive import ExclusiveLocks
from .garage import can_configure_garage, can_set_lock_mode, supports_garage_settings
from .locks import has_remote_control, is_lock, lock_online, uses_action_api
from .models import Device, nested
from .video import Video

WORKFLOWS = "https://workflows.myq-cloud.com"
GUESTACCESS = "https://guestaccess.myq-cloud.com"
AUTO_RELOCK_SECONDS = (30, 60, 90, 120, 180)
ACCESSORY_NAME_LENGTH = 24
# iv0/c.b: the guest device pickers (wk0/a.g) offer only these types, or a device that
# reports a camera, and only serials containing one of these markers.
SHAREABLE_TYPES = frozenset(
    {
        "garagedooropener",
        "wifigaragedooropener",
        "virtualgaragedooropener",
        "commercialdooropener",
        "vkpgateway",
        "lockitron",
        "gate",
        "lamp",
        "wifilockgateway",
    }
)
SHAREABLE_SERIALS = ("CG06", "CG08", "CG0A", "LTRON", "GW", "CG28", "CG091")
AUTO_LOCK_SECONDS = (0, *AUTO_RELOCK_SECONDS)
SCHEDULE_FEATURES = frozenset({"floodlight", "patrol_mode", "privacy_mode", "voice_deter"})
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TRANSMITTER_ROUTES = {
    "OokTx": "OokTx",
    "FhssTx": "FhssTxs",
    "S3Transmitter": "S3Transmitters",
    "S3Keypad": "S3Keypads",
}


def can_auto_relock(
    device: Device | None, access: AccountAccess | None = None
) -> TypeGuard[Device]:
    """Legacy remote timer only; Wi-Fi/Seam use a different configuration API."""
    return (
        device is not None
        and is_lock(device)
        and not uses_action_api(device)
        and has_remote_control(device)
        and lock_online(device)
        and device.state.get("allow_auto_relock") is True
        and not device.monitor_only
        and type(device.state.get("auto_relock_enabled")) is bool
        and type(device.state.get("auto_relock_time")) is int
        and device.state["auto_relock_time"] in AUTO_RELOCK_SECONDS
        and access is not None
        and access.owner
    )


def can_auto_lock(device: Device | None, access: AccountAccess | None = None) -> TypeGuard[Device]:
    """Modern native lock gateway settings, distinct from vendor Seam locks."""
    return (
        device is not None
        and device.device_type == "wifilockgateway"
        and lock_online(device)
        and not device.monitor_only
        and device.state.get("automatic_lock_state") in ("enabled", "disabled")
        and (
            device.state.get("automatic_lock_state") == "disabled"
            or (
                type(device.state.get("automatic_lock_delay_duration")) is int
                and device.state["automatic_lock_delay_duration"] in AUTO_RELOCK_SECONDS
            )
        )
        # The HPP lock settings provider shows these to owners and co-owners (y80/p, y80/h).
        and access is not None
        and access.owner_or_co_owner
    )


def can_configure_door_sensor(
    device: Device | None, access: AccountAccess | None = None
) -> TypeGuard[Device]:
    """A configured native sensor can be enabled/disabled without calibration."""
    return (
        can_auto_lock(device, access)
        and isinstance(device.state.get("door_sensor_state"), str)
        and device.state["door_sensor_state"].lower() in ("enabled", "disabled")
        and isinstance(device.state.get("door_sensor_calibration_status"), str)
        and device.state["door_sensor_calibration_status"].lower() == "calibrated"
    )


def can_reset_pin_lockout(
    device: Device | None, access: AccountAccess | None = None
) -> TypeGuard[Device]:
    """The app offers the clear flow on the device tile (sx/z, c70/a) with no role check;
    owners and co-owners are allowed here."""
    return (
        device is not None
        and device.online
        and type(device.state.get("failed_credential_lockout_active")) is bool
        and access is not None
        and access.owner_or_co_owner
    )


def lockout_target(device: Device) -> str:
    """Device.s() (yc1/f): gateway_id, state.gatewayID, parent_serial_number, then own."""
    parent = device.state.get("parent_serial_number")
    return device.gateway_id or (parent if isinstance(parent, str) and parent else device.serial)


def rename_route(device: Device) -> tuple[str, str]:
    """RenameDeviceServiceImpl (yy0/k.J2) routes by type and family; body is {"name"}."""
    account, serial = _identifier(device.account_id), _identifier(device.serial)
    if device.device_type == "lamp":
        return LAMPS, f"/api/v6.0/accounts/{account}/lamps/{serial}"
    if device.family == "garagedoor":
        return GDO, f"/api/v6.0/accounts/{account}/door_openers/{serial}"
    if device.device_type == "vkpgateway":
        # account-devices-vkp: the route version was not traced.
        raise CommandRejected("Renaming this video keypad is not supported")
    if device.family == "gateway" and device.device_type != "wifilockgateway":
        return GATEWAYS, f"/api/v6.0/accounts/{account}/gateways/{serial}"
    return DEVICES, f"/api/v6.0/Accounts/{account}/Devices/{serial}"


def can_create_workflow(device: Device, feature: str, details: dict) -> bool:
    """Native first-scene entry gates, with explicit camera capability evidence."""
    if not device.is_camera or not device.online:
        return False
    if feature == "privacy_mode":
        # c60/s3 exposes SleepSchedules for k.q1() (CAMERA_PTZ) and enableSwitch.
        return (
            device.device_type == "smartptzcam1"
            and nested(details, "cap", "cam", "enableSwitch") is True
        )
    if feature == "voice_deter":
        return nested(details, "cap", "cam", "voiceDeter") is True
    if feature == "floodlight":
        caps = nested(details, "cap", "lightList")
        return isinstance(caps, list) and any(
            isinstance(item, dict)
            and item.get("type") == "floodlight"
            and isinstance(item.get("switchStateList"), list)
            and "on" in item["switchStateList"]
            and "off" in item["switchStateList"]
            for item in caps
        )
    if feature == "patrol_mode":
        # r70/k0 -> l00/b -> xz/q.b: patrol requires two distinct saved presets.
        presets = nested(details, "settings", "cam", "ptz", "presets")
        maximum = nested(details, "cap", "cam", "ptz", "maxPresets")
        if (
            device.device_type != "smartptzcam1"
            or type(maximum) is not int
            or not 2 <= maximum <= 256
            or not isinstance(presets, list)
            or not 2 <= len(presets) <= maximum
            or nested(details, "settings", "cam", "ptz", "presetDrifted") is True
            or any(
                not isinstance(p, dict) or type(p.get("id")) is not int or p["id"] < 0
                for p in presets
            )
        ):
            return False
        return len({p["id"] for p in presets}) >= 2
    return False


def _identifier(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or len(value) > 256
        or any(ord(char) < 32 for char in value)
    ):
        raise CommandRejected("Invalid MyQ management identifier")
    return quote(value, safe="")


def _name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 64
        or any(ord(char) < 32 for char in value)
    ):
        raise CommandRejected("A management name must contain 1 to 64 printable characters")
    return value


def _transmitter_route(accessory: Accessory) -> str:
    # Native TransmitterUtils chooses FHSS only for HomeLink serial prefixes
    # TX02/TX0C; other HomeLink entries use the OOK route.
    if accessory.transmitter_type == "FhssTx" and not accessory.serial.startswith(("TX02", "TX0C")):
        return "OokTx"
    return _TRANSMITTER_ROUTES[accessory.transmitter_type]


@dataclass(frozen=True)
class Accessory:
    serial: str = field(repr=False)
    name: str = field(repr=False)
    transmitter_type: str
    deleting: bool
    battery: str
    account_user_id: str | None = field(default=None, repr=False)
    gateway_serial: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WorkflowSchedule:
    configuration_id: str = field(repr=False)
    id: str = field(repr=False)
    feature: str
    label: str = field(repr=False)
    enabled: bool
    days: str = field(repr=False)
    start_time: str = field(repr=False)
    end_time: str = field(repr=False)


@dataclass(frozen=True)
class PinMetadata:
    """The native list DTO contains metadata only, never the numeric PIN."""

    id: str = field(repr=False)
    name: str = field(repr=False)
    access: str = field(repr=False)
    status: str


@dataclass(frozen=True)
class GuestAccessSchedule:
    id: str = field(repr=False)
    allowed_devices: tuple[str, ...] = field(repr=False)
    settings: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class Guest:
    id: str = field(repr=False)
    name: str = field(repr=False)
    role: str
    schedules: tuple[GuestAccessSchedule, ...] = field(repr=False)


class ReportedFailure(CommandRejected):
    """MyQ reported during confirmation that a change failed; not an unknown outcome."""


def shareable(device: Device) -> bool:
    """Whether the app's guest screens let an owner give a guest this device."""
    return (
        device.device_type in SHAREABLE_TYPES or device.state.get("has_camera") is True
    ) and any(marker in device.serial for marker in SHAREABLE_SERIALS)


def _rows(data: Any, key: str = "items") -> list[dict[str, Any]]:
    """Workflow lists as the app reads them: a null list is empty, count is not read."""
    if not isinstance(data, dict):
        raise ProtocolError("MyQ workflow response is invalid")
    rows = data.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ProtocolError("MyQ workflow response is invalid")
    return [row for row in rows if isinstance(row, dict)]


WEEK_MINUTES = 7 * 24 * 60


def _windows(days: str, start: str, end: str) -> list[tuple[int, int]]:
    """Minutes of the week a schedule covers; an end not after its start runs past midnight."""

    def minutes(value: str) -> int | None:
        parts = value.split(":")
        if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
            return None
        return int(parts[0]) * 60 + int(parts[1])

    begin, finish = minutes(start), minutes(end)
    if begin is None or finish is None:
        return []
    length = finish - begin if finish > begin else finish - begin + 24 * 60
    windows = []
    for name in (part.strip().lower() for part in days.split(",")):
        if name in WEEKDAYS:
            offset = WEEKDAYS.index(name) * 24 * 60 + begin
            windows.append((offset, offset + length))
    return windows


def _overlaps(schedule: dict[str, Any], other: WorkflowSchedule) -> bool:
    mine = _windows(schedule["days"], schedule["start_time"], schedule["end_time"])
    theirs = _windows(other.days, other.start_time, other.end_time)
    for a_start, a_end in mine:
        for b_start, b_end in theirs:
            # Compare across the week boundary too (Sunday night into Monday).
            for shift in (-WEEK_MINUTES, 0, WEEK_MINUTES):
                if a_start < b_end + shift and b_start + shift < a_end:
                    return True
    return False


def _items(data: Any, key: str = "items") -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ProtocolError("MyQ management inventory is invalid")
    items = data[key]
    if any(not isinstance(item, dict) for item in items):
        raise ProtocolError("MyQ management inventory is invalid")
    count = data.get("count", len(items))
    if type(count) is not int or count != len(items):
        raise ProtocolError("MyQ management inventory is incomplete")
    return items


class Management:
    def __init__(
        self, client: Client, video: Video | None = None, user_id: str | None = None
    ) -> None:
        self.client = client
        # The signed-in profile's user ID: accounts it created are its own (h0.k).
        self.user_id = user_id
        self.video = video if video is not None else Video(client)
        self._locks = ExclusiveLocks()
        self._configuration_ids: dict[tuple[str, str], frozenset[str]] = {}
        self.poll_interval = 5
        self.poll_attempts = 12

    @asynccontextmanager
    async def _operation(self, device: Device, *, account_scope: bool = False):
        key = f"{device.account_id}:guest-management" if account_scope else device.key
        async with self._locks.hold(key, "A management operation is already in progress"):
            yield

    async def account_access(self) -> dict[str, AccountAccess]:
        response = await self.client.request("GET", ACCOUNTS, "/api/v6.0/accountAccess")
        return parse_account_access(response.json(), self.owned_accounts())

    def owned_accounts(self) -> frozenset[str]:
        """Accounts whose created_by is the signed-in user, from the last accounts list."""
        if not self.user_id:
            return frozenset()
        return frozenset(
            account.id
            for account in self.client.known_accounts
            if account.created_by == self.user_id
        )

    def owner_access(self) -> dict[str, AccountAccess]:
        """Ownership is known even when AccountAccess cannot be read, as in the app."""
        return owned_access(self.owned_accounts())

    async def require_access(
        self,
        account_id: str,
        resource: str,
        permission: str,
        *,
        owner: bool = False,
        co_owner: bool = False,
    ) -> AccountAccess:
        """The app's role gate for this operation, from a fresh AccountAccess read.

        owner requires the account owner; co_owner also accepts a co-owner.
        """
        access = (await self.account_access()).get(account_id)
        if (
            access is None
            or not access.allows(resource, permission)
            or (owner and not (access.owner_or_co_owner if co_owner else access.owner))
        ):
            raise CommandRejected("Current MyQ account permissions do not allow this operation")
        return access

    async def _current(self, device: Device) -> Device:
        for current in await self.client.devices(device.account_id):
            if current.key == device.key:
                return current
        raise CommandRejected("Device is no longer in the MyQ account")

    async def _confirm(self, check: Callable[[], Awaitable[bool]]) -> None:
        unreachable = False
        for attempt in range(self.poll_attempts):
            if attempt:
                await asyncio.sleep(self.poll_interval)
            try:
                if await check():
                    return
                unreachable = False
            except ReportedFailure:
                raise
            except CannotConnect:
                # One lost read-back is not an outcome; the remaining attempts decide.
                unreachable = True
            except MyQError:
                raise CommandUncertain(
                    "MyQ accepted the management request but confirmation is unavailable"
                ) from None
        if unreachable:
            raise CommandUncertain(
                "MyQ accepted the management request but confirmation is unavailable"
            )
        raise CommandUncertain("MyQ has not confirmed the management change; do not replay it")

    async def set_lock_mode(self, device: Device, enabled: bool) -> None:
        """Lock mode disables the opener's remotes and keypads (c2 -> wz0/a lockmode)."""
        if type(enabled) is not bool:
            raise CommandRejected("Lock mode must be a Boolean")
        async with self._operation(device):
            current = await self._current(device)
            access = await self.require_access(device.account_id, "devices", "Manage", owner=True)
            if not can_set_lock_mode(current, access):
                raise CommandRejected("This opener does not offer lock mode")
            await self.client.device_mutation(
                current,
                "PUT",
                GDO,
                self._accessory_path(current) + "/lockmode",
                json={"enable_lock_mode": enabled},
            )

            async def confirmed() -> bool:
                # iv0/b.g reads a null in_vacation_mode as false.
                return ((await self._current(current)).state.get("in_vacation_mode") is True) is (
                    enabled
                )

            await self._confirm(confirmed)

    async def reset_pin_lockout(self, device: Device) -> None:
        """POST residential-credentials v1.0 .../lockout/reset (xt0/a, wt0/r)."""
        async with self._operation(device):
            current = await self._current(device)
            access = await self.require_access(
                device.account_id, "devices", "Manage", owner=True, co_owner=True
            )
            if (
                not can_reset_pin_lockout(current, access)
                or current.state.get("failed_credential_lockout_active") is not True
            ):
                raise CommandRejected("No keypad lockout is active on this device")
            await self.client.device_mutation(
                current,
                "POST",
                CREDENTIALS,
                f"/api/v1.0/accounts/{_identifier(device.account_id)}/devices/"
                f"{_identifier(lockout_target(current))}/lockout/reset",
                json={"credential_type": "pin"},
            )

            async def confirmed() -> bool:
                state = (await self._current(current)).state
                return state.get("failed_credential_lockout_active") is False

            await self._confirm(confirmed)

    async def rename_device(self, device: Device, name: str) -> None:
        """Owner-only rename, limited to the app's 24 characters (y80/o)."""
        name = _name(name).strip()
        if not name or len(name) > 24:
            raise CommandRejected("A device name must contain 1 to 24 characters")
        async with self._operation(device):
            current = await self._current(device)
            await self.require_access(device.account_id, "devices", "Manage", owner=True)
            base, path = rename_route(current)
            await self.client.device_mutation(current, "PUT", base, path, json={"name": name})

            async def confirmed() -> bool:
                return (await self._current(current)).name == name

            await self._confirm(confirmed)

    async def set_auto_relock(self, device: Device, enabled: bool, seconds: int) -> None:
        if (
            type(enabled) is not bool
            or type(seconds) is not int
            or seconds not in AUTO_RELOCK_SECONDS
        ):
            raise CommandRejected("Unsupported auto relock setting")
        async with self._operation(device):
            current = await self._current(device)
            access = await self.require_access(device.account_id, "devices", "Manage", owner=True)
            if not can_auto_relock(current, access):
                raise CommandRejected("This lock does not allow remote auto relock configuration")
            path = (
                f"/api/v6.0/accounts/{_identifier(device.account_id)}/devices/locks/"
                f"{_identifier(device.serial)}/auto_relock"
            )
            await self.client.device_mutation(
                current,
                "PUT",
                LOCKS,
                path,
                params={"requestRemote": "true"},
                json={"auto_relock_enabled": enabled, "auto_relock_time": str(seconds)},
            )

            async def confirmed() -> bool:
                observed = await self._current(device)
                return (
                    observed.state.get("auto_relock_enabled") is enabled
                    and observed.state.get("auto_relock_time") == seconds
                )

            await self._confirm(confirmed)

    async def set_auto_lock(self, device: Device, seconds: int) -> None:
        """0 disables; positive native options enable and set the delay."""
        if type(seconds) is not int or seconds not in AUTO_LOCK_SECONDS:
            raise CommandRejected("Unsupported native automatic lock delay")
        async with self._operation(device):
            current = await self._current(device)
            access = await self.require_access(
                device.account_id, "devices", "Manage", owner=True, co_owner=True
            )
            if not can_auto_lock(current, access):
                raise CommandRejected("This device does not permit native automatic lock settings")
            # r80/h.f: null delay on Disable is omitted by the native Gson client.
            payload: dict[str, Any] = {"automatic_lock_state": "Enable" if seconds else "Disable"}
            if seconds:
                payload["automatic_lock_delay_duration"] = seconds
            await self.client.device_mutation(
                current,
                "PUT",
                ACTIONS,
                f"/api/v1/accounts/{_identifier(device.account_id)}/devices/"
                f"{_identifier(current.serial)}/lockconfiguration",
                json=payload,
            )

            async def confirmed() -> bool:
                state = (await self._current(current)).state
                return state.get("automatic_lock_state") == (
                    "enabled" if seconds else "disabled"
                ) and (not seconds or state.get("automatic_lock_delay_duration") == seconds)

            await self._confirm(confirmed)

    async def set_door_sensor(self, device: Device, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise CommandRejected("Door sensor state must be a boolean")
        async with self._operation(device):
            current = await self._current(device)
            access = await self.require_access(
                device.account_id, "devices", "Manage", owner=True, co_owner=True
            )
            if not can_configure_door_sensor(current, access):
                raise CommandRejected("This lock has no calibrated configurable door sensor")
            await self.client.device_mutation(
                current,
                "PUT",
                ACTIONS,
                f"/api/v1/accounts/{_identifier(device.account_id)}/devices/"
                f"{_identifier(current.serial)}/lockconfiguration",
                json={"door_sensor_state": "Enable" if enabled else "Disable"},
            )

            async def confirmed() -> bool:
                state = (await self._current(current)).state.get("door_sensor_state")
                return isinstance(state, str) and state.lower() == (
                    "enabled" if enabled else "disabled"
                )

            await self._confirm(confirmed)

    @staticmethod
    def _accessory_path(device: Device) -> str:
        return (
            f"/api/v6.0/accounts/{_identifier(device.account_id)}/door_openers/"
            f"{_identifier(device.serial)}"
        )

    async def accessories(self, device: Device) -> tuple[Accessory, ...]:
        if not supports_garage_settings(device):
            return ()
        response = await self.client.request(
            "GET", GDO, self._accessory_path(device) + "/accessories"
        )
        data = response.json()
        if not isinstance(data, dict):
            raise ProtocolError("MyQ accessory response is invalid")
        # yz0/g.a: a null list is empty, a missing flag false and a missing name empty;
        # the opener serial is not checked. Only a contradicting serial is refused here.
        reported = data.get("gdo_serial_number")
        if isinstance(reported, str) and reported and reported != device.serial:
            raise ProtocolError("MyQ accessory response does not match the requested opener")
        items = data.get("gdo_transmitter_names")
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ProtocolError("MyQ accessory response is invalid")
        result, seen = [], set()
        for item in items:
            serial = item.get("tx_serial_number") if isinstance(item, dict) else None
            if not isinstance(serial, str) or not serial or serial in seen:
                continue  # Without a unique serial an accessory cannot be addressed.
            seen.add(serial)
            kind, name = item.get("transmitter_type"), item.get("name")
            battery = item.get("battery_state")
            result.append(
                Accessory(
                    serial,
                    name if isinstance(name, str) else "",
                    kind if isinstance(kind, str) else "",
                    item.get("is_delete_in_progress") is True,
                    battery
                    if isinstance(battery, str) and battery in {"good", "low", "critical"}
                    else "unknown",
                    item.get("account_user_id")
                    if isinstance(item.get("account_user_id"), str)
                    else None,
                    item.get("gateway_serial_number")
                    if isinstance(item.get("gateway_serial_number"), str)
                    else None,
                )
            )
        return tuple(result)

    async def _accessory(
        self, device: Device, serial: str, inventory: tuple[Accessory, ...] | None = None
    ) -> Accessory:
        _identifier(serial)
        for accessory in inventory if inventory is not None else await self.accessories(device):
            if accessory.serial == serial:
                if accessory.deleting or accessory.transmitter_type not in _TRANSMITTER_ROUTES:
                    raise CommandRejected(
                        "This accessory does not permit this management operation"
                    )
                return accessory
        raise CommandRejected("Accessory is no longer paired with this opener")

    async def rename_accessory(self, device: Device, accessory_serial: str, name: str) -> None:
        name = _name(name)
        # s50/e1.m0 and c0: at most 24 characters, unique on the opener ignoring case.
        if len(name) > ACCESSORY_NAME_LENGTH:
            raise CommandRejected("An accessory name can have at most 24 characters")
        async with self._operation(device):
            current = await self._current(device)
            if not supports_garage_settings(current):
                raise CommandRejected("Opener does not currently permit accessory management")
            inventory = await self.accessories(current)
            accessory = await self._accessory(current, accessory_serial, inventory)
            if any(
                other.serial != accessory.serial and other.name.casefold() == name.casefold()
                for other in inventory
            ):
                raise CommandRejected("Another accessory on this opener already has that name")
            access = await self.require_access(device.account_id, "devices", "Manage", owner=True)
            if not can_configure_garage(current, access):
                raise CommandRejected("Opener does not currently permit accessory management")
            route = _transmitter_route(accessory)
            await self.client.device_mutation(
                current,
                "PUT",
                GDO,
                self._accessory_path(current) + f"/{route}/rename",
                json={
                    "name": name,
                    "tx_serial_number": accessory.serial,
                    "is_delete_in_progress": False,
                },
            )

            async def confirmed() -> bool:
                return any(
                    a.serial == accessory.serial and a.name == name
                    for a in await self.accessories(current)
                )

            await self._confirm(confirmed)

    async def remove_accessory(self, device: Device, accessory_serial: str) -> None:
        async with self._operation(device):
            current = await self._current(device)
            if not supports_garage_settings(current):
                raise CommandRejected("Opener does not currently permit accessory management")
            accessory = await self._accessory(current, accessory_serial)
            access = await self.require_access(device.account_id, "devices", "Delete", owner=True)
            if not can_configure_garage(current, access):
                raise CommandRejected("Opener does not currently permit accessory management")
            route = _transmitter_route(accessory)
            await self.client.device_mutation(
                current,
                "PUT",
                GDO,
                self._accessory_path(current)
                + f"/{route}/{_identifier(accessory.serial)}/send_remove",
            )

            async def confirmed() -> bool:
                return all(a.serial != accessory.serial for a in await self.accessories(current))

            await self._confirm(confirmed)

    @staticmethod
    def _scene_path(device: Device) -> str:
        return f"/api/v1.0/accounts/{_identifier(device.account_id)}/workflows/scenes"

    async def schedules(self, device: Device, feature: str) -> tuple[WorkflowSchedule, ...]:
        if not isinstance(feature, str) or feature not in SCHEDULE_FEATURES or not device.is_camera:
            raise CommandRejected("Unsupported native workflow feature")
        response = await self.client.request(
            "GET",
            WORKFLOWS,
            self._scene_path(device),
            headers=NATIVE_HEADERS,
            params={"serialNumber": device.serial, "feature": feature},
        )
        result, seen, configurations = [], set(), set()
        for scene in _rows(response.json()):
            configuration_id = scene.get("id") or scene.get("configuration_id")
            if (
                not isinstance(configuration_id, str)
                or not configuration_id
                or configuration_id in configurations
            ):
                raise ProtocolError("MyQ workflow configuration has no identity")
            configurations.add(configuration_id)
            for item in _rows(scene, "schedules"):
                schedule_id = item.get("id")
                if (
                    not isinstance(schedule_id, str)
                    or not schedule_id
                    or (configuration_id, schedule_id) in seen
                ):
                    raise ProtocolError("MyQ workflow schedule is invalid")
                seen.add((configuration_id, schedule_id))
                # c21/f.d: missing text reads as empty and a missing flag as disabled.
                text = {
                    key: item[key] if isinstance(item.get(key), str) else ""
                    for key in ("label", "days", "start_time", "end_time")
                }
                result.append(
                    WorkflowSchedule(
                        configuration_id,
                        schedule_id,
                        feature,
                        text["label"],
                        item.get("enabled") is True,
                        text["days"],
                        text["start_time"],
                        text["end_time"],
                    )
                )
        self._configuration_ids[(device.key, feature)] = frozenset(configurations)
        return tuple(result)

    def configuration_ids(self, device: Device, feature: str) -> tuple[str, ...]:
        """IDs from the latest successful schedules read, including empty scenes.

        Call ``await schedules(device, feature)`` immediately before using this
        for a service response. Mutations always perform their own fresh read.
        """
        if not isinstance(feature, str) or feature not in SCHEDULE_FEATURES or not device.is_camera:
            raise CommandRejected("Unsupported native workflow feature")
        return tuple(sorted(self._configuration_ids.get((device.key, feature), ())))

    async def _schedule_target(
        self, device: Device, feature: str, configuration_id: str, schedule_id: str | None
    ) -> tuple[WorkflowSchedule, ...]:
        _identifier(configuration_id)
        if schedule_id is not None:
            _identifier(schedule_id)
        schedules = await self.schedules(device, feature)
        matching = tuple(s for s in schedules if s.configuration_id == configuration_id)
        if configuration_id not in self._configuration_ids.get((device.key, feature), ()) or (
            schedule_id is not None and not any(s.id == schedule_id for s in matching)
        ):
            raise CommandRejected("Workflow schedule is no longer associated with this device")
        return matching

    async def _schedule_write(
        self,
        device: Device,
        feature: str,
        configuration_id: str,
        schedule_id: str | None,
        payload: dict | Callable[[WorkflowSchedule], dict] | None,
    ) -> None:
        async with self._operation(device):
            current = await self._current(device)
            before = await self._schedule_target(current, feature, configuration_id, schedule_id)
            if callable(payload):
                payload = payload(next(s for s in before if s.id == schedule_id))
            if payload is not None and payload.get("enabled") is True:
                # l60/t.f0 and k0 check an enabled schedule against the others (l60/l1.b).
                others = [s for s in before if s.enabled and s.id != schedule_id]
                if any(_overlaps(payload, other) for other in others):
                    raise CommandRejected("The schedule overlaps another enabled schedule")
            permission = "Delete" if payload is None else "Manage"
            await self.require_access(device.account_id, "schedules", permission)
            path = self._scene_path(device) + f"/{_identifier(configuration_id)}/schedules"
            if schedule_id is not None:
                path += f"/{_identifier(schedule_id)}"
            method = "DELETE" if payload is None else ("POST" if schedule_id is None else "PUT")
            kwargs = {"json": payload} if payload is not None else {}
            await self.client.device_mutation(
                current, method, WORKFLOWS, path, headers=NATIVE_HEADERS, **kwargs
            )

            async def confirmed() -> bool:
                after = await self.schedules(current, feature)
                candidates = [
                    s
                    for s in after
                    if s.configuration_id == configuration_id
                    and (
                        s.id == schedule_id
                        if schedule_id is not None
                        else s.id not in {old.id for old in before}
                    )
                ]
                if payload is None:
                    return not candidates
                return any(
                    all(getattr(s, key) == value for key, value in payload.items())
                    for s in candidates
                )

            await self._confirm(confirmed)

    async def set_schedule_enabled(
        self, device: Device, feature: str, configuration_id: str, schedule_id: str, enabled: bool
    ) -> None:
        if type(enabled) is not bool:
            raise CommandRejected("Workflow enabled state must be a boolean")

        def toggled(existing: WorkflowSchedule) -> dict:
            # The app's only schedule PUT (l60/k2 -> c21/f) sends all five fields; a
            # partial body could replace the schedule's days and times.
            return {
                "label": existing.label,
                "enabled": enabled,
                "days": existing.days,
                "start_time": existing.start_time,
                "end_time": existing.end_time,
            }

        await self._schedule_write(device, feature, configuration_id, schedule_id, toggled)

    async def save_schedule(
        self,
        device: Device,
        feature: str,
        configuration_id: str,
        *,
        label: str,
        days: list[str],
        start_time: str,
        end_time: str,
        enabled: bool,
        schedule_id: str | None = None,
    ) -> None:
        payload = self._schedule_payload(label, days, start_time, end_time, enabled)
        await self._schedule_write(device, feature, configuration_id, schedule_id, payload)

    @staticmethod
    def _schedule_payload(
        label: str, days: list[str], start_time: str, end_time: str, enabled: bool
    ) -> dict:
        label = _name(label)
        if (
            not isinstance(days, list)
            or not days
            or any(day not in WEEKDAYS for day in days)
            or len(set(days)) != len(days)
            or type(enabled) is not bool
            or any(
                not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:00", t)
                for t in (start_time, end_time)
            )
            or start_time == end_time
        ):
            raise CommandRejected("Invalid native workflow days or time window")
        return {
            "label": label,
            "enabled": enabled,
            "days": ", ".join(day for day in WEEKDAYS if day in days),
            "start_time": start_time,
            "end_time": end_time,
        }

    async def create_schedule(
        self,
        device: Device,
        feature: str,
        *,
        label: str,
        days: list[str],
        start_time: str,
        end_time: str,
        enabled: bool,
    ) -> None:
        """Create a first native workflow configuration and its first schedule."""
        payload = self._schedule_payload(label, days, start_time, end_time, enabled)
        if not isinstance(feature, str) or feature not in SCHEDULE_FEATURES:
            raise CommandRejected("Unsupported native workflow feature")
        async with self._operation(device):
            current = await self._current(device)
            await self.require_access(device.account_id, "schedules", "Manage")
            details = await self.video.details(current)
            if not can_create_workflow(current, feature, details):
                raise CommandRejected(
                    "Camera has no confirmed eligibility for this workflow feature"
                )
            await self.schedules(current, feature)
            if self.configuration_ids(current, feature):
                raise CommandRejected("Use the existing workflow configuration for this feature")
            await self.require_access(device.account_id, "schedules", "Manage")
            body = {
                "enabled": enabled,
                "action": {
                    "devices": [
                        {"enabled": True, "serial_number": current.serial, "feature": feature}
                    ]
                },
                "schedules": [payload],
            }
            response = await self.client.device_mutation(
                current,
                "POST",
                WORKFLOWS,
                self._scene_path(current),
                headers=NATIVE_HEADERS,
                json=body,
            )
            try:
                data = response.json()
                configuration_id = (
                    (data.get("id") or data.get("configuration_id"))
                    if isinstance(data, dict)
                    else None
                )
                if not isinstance(configuration_id, str) or not configuration_id:
                    raise ProtocolError("Workflow creation did not return a configuration identity")
            except ProtocolError:
                raise CommandUncertain(
                    "Workflow creation was submitted but cannot be confirmed"
                ) from None

            async def confirmed() -> bool:
                return any(
                    s.configuration_id == configuration_id
                    and all(getattr(s, key) == value for key, value in payload.items())
                    for s in await self.schedules(current, feature)
                )

            await self._confirm(confirmed)

    async def delete_schedule(
        self, device: Device, feature: str, configuration_id: str, schedule_id: str
    ) -> None:
        await self._schedule_write(device, feature, configuration_id, schedule_id, None)

    async def pins(self, device: Device) -> tuple[PinMetadata, ...]:
        if (
            not is_lock(device)
            or uses_action_api(device)
            or device.state.get("allow_pin_code") is not True
        ):
            raise CommandRejected("This lock does not expose the native PIN inventory")
        await self.require_access(device.account_id, "guests", "View")
        return await self._pins(device)

    @staticmethod
    def _pins_path(device: Device) -> str:
        return (
            f"/api/v6.0/accounts/{_identifier(device.account_id)}/devices/locks/"
            f"{_identifier(device.serial)}/pins"
        )

    async def _pins(self, device: Device) -> tuple[PinMetadata, ...]:
        response = await self.client.request("GET", LOCKS, self._pins_path(device))
        result, seen = [], set()
        for item in _items(response.json()):
            if (
                any(not isinstance(item.get(k), str) for k in ("id", "name", "access", "status"))
                or not item["id"]
                or item["id"] in seen
            ):
                raise ProtocolError("MyQ PIN metadata is invalid")
            seen.add(item["id"])
            status = item["status"].lower()
            if status not in {"pending", "synced", "duplicate", "failed"}:
                status = "unknown"
            result.append(PinMetadata(item["id"], item["name"], item["access"], status))
        return tuple(result)

    async def _pin_device(self, device: Device, permission: str) -> Device:
        current = await self._current(device)
        if (
            not is_lock(current)
            or uses_action_api(current)
            or not has_remote_control(current)
            or not lock_online(current)
            or current.state.get("allow_pin_code") is not True
            or current.monitor_only
        ):
            raise CommandRejected("This lock does not permit remote PIN management")
        await self._require_pin_access(device, permission)
        return current

    async def _require_pin_access(self, device: Device, permission: str) -> None:
        # b70/x0.r0: allow_pin_code and any role but guest (the guests gate).
        await self.require_access(device.account_id, "guests", permission)

    async def save_pin(
        self, device: Device, *, name: str, pin: str, pin_id: str | None = None
    ) -> None:
        """Create or replace a legacy remote PIN; credential is never retained."""
        name = _name(name)
        # Native validator requires at least four digits. The 16-digit upper
        # bound is a local input bound; model-specific limits remain server-enforced.
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,16}", pin):
            raise CommandRejected("PIN must contain 4 to 16 decimal digits")
        if pin_id is not None:
            _identifier(pin_id)
        async with self._operation(device):
            current = await self._pin_device(device, "Manage")
            before = await self._pins(current)
            if pin_id is not None:
                old = next((item for item in before if item.id == pin_id), None)
                if old is None or old.access != "enabled" or old.status != "synced":
                    raise CommandRejected("Existing PIN is unavailable or not ready for management")
            elif any(item.name.casefold() == name.casefold() for item in before):
                raise CommandRejected("A PIN with that name already exists")
            await self._require_pin_access(device, "Manage")
            path = self._pins_path(current)
            payload = {"name": name, "access": "enabled"}
            if pin_id is None:
                method, credential_key = "POST", "pin"
            else:
                method, credential_key = "PUT", "updated_pin"
                path += f"/{_identifier(pin_id)}"
            payload[credential_key] = pin
            try:
                response = await self.client.device_mutation(
                    current, method, LOCKS, path, params={"requestRemote": "true"}, json=payload
                )
            finally:
                payload.pop(credential_key, None)
                pin = ""
            try:
                data = response.json()
                returned_id = data.get("id") if isinstance(data, dict) else None
                if (
                    not isinstance(returned_id, str)
                    or not returned_id
                    or (pin_id is not None and returned_id != pin_id)
                    or (pin_id is None and returned_id in {item.id for item in before})
                ):
                    raise ProtocolError("MyQ PIN mutation response has no matching identity")
            except ProtocolError:
                raise CommandUncertain(
                    "MyQ PIN request was submitted but cannot be confirmed"
                ) from None

            # Existing PIN metadata alone cannot identify a new credential.
            # Require readback to observe pending -> synced for a replacement (the
            # PUT response itself may already say pending, as c11.Pin does); an
            # unobserved transition remains uncertain, never replayed.
            status = data.get("status")
            saw_pending = pin_id is None or (
                isinstance(status, str) and status.lower() == "pending"
            )
            unreachable = False
            for attempt in range(self.poll_attempts):
                if attempt:
                    await asyncio.sleep(self.poll_interval)
                try:
                    observed = next(
                        (item for item in await self._pins(current) if item.id == returned_id), None
                    )
                except CannotConnect:
                    unreachable = True  # Transient; the remaining attempts decide.
                    continue
                except MyQError:
                    raise CommandUncertain("MyQ PIN confirmation is unavailable") from None
                unreachable = False
                if observed is not None:
                    if observed.status == "pending":
                        saw_pending = True
                    if (
                        saw_pending
                        and observed.status == "synced"
                        and observed.name == name
                        and observed.access == "enabled"
                    ):
                        return
                    if observed.status in {"duplicate", "failed"}:
                        raise CommandRejected("MyQ reported that the PIN change failed")
            if unreachable:
                raise CommandUncertain("MyQ PIN confirmation is unavailable")
            raise CommandUncertain("MyQ has not confirmed PIN synchronization; do not replay it")

    async def delete_pin(self, device: Device, pin_id: str) -> None:
        _identifier(pin_id)
        async with self._operation(device):
            current = await self._pin_device(device, "Delete")
            if not any(item.id == pin_id for item in await self._pins(current)):
                raise CommandRejected("PIN is no longer associated with this lock")
            await self._require_pin_access(device, "Delete")
            await self.client.device_mutation(
                current,
                "DELETE",
                LOCKS,
                self._pins_path(current) + f"/{_identifier(pin_id)}",
                params={"requestRemote": "true"},
            )

            async def confirmed() -> bool:
                remaining = next(
                    (item for item in await self._pins(current) if item.id == pin_id), None
                )
                # l31/d stops when the PIN's status becomes failed.
                if remaining is not None and remaining.status == "failed":
                    raise ReportedFailure("MyQ reported that the PIN deletion failed")
                return remaining is None

            await self._confirm(confirmed)

    @staticmethod
    def _guests_path(device: Device) -> str:
        return f"/api/v6.0/accounts/{_identifier(device.account_id)}/users"

    async def guests(self, device: Device) -> tuple[Guest, ...]:
        await self.require_access(device.account_id, "guests", "View")
        return await self._guests(device)

    async def _guests(self, device: Device) -> tuple[Guest, ...]:
        response = await self.client.request(
            "GET", GUESTACCESS, self._guests_path(device), headers=NATIVE_HEADERS
        )
        result, seen = [], set()
        for item in _items(response.json(), "account_users"):
            guest_id, role = item.get("id"), item.get("role")
            if not isinstance(guest_id, str) or not guest_id or guest_id in seen:
                raise ProtocolError("MyQ guest inventory contains an ambiguous identity")
            seen.add(guest_id)
            if role not in ("owner", "co_owner", "guest", "family", "guest_nomyquser"):
                role = "unknown"
            schedules, schedule_ids = [], set()
            raw_schedules = item.get("access_schedules") or []
            if not isinstance(raw_schedules, list):
                raise ProtocolError("MyQ guest access schedules are invalid")
            for schedule in raw_schedules:
                if not isinstance(schedule, dict):
                    raise ProtocolError("MyQ guest access schedule is invalid")
                schedule_id = schedule.get("id")
                allowed = schedule.get("allowed_devices")
                if (
                    not isinstance(schedule_id, str)
                    or not schedule_id
                    or schedule_id in schedule_ids
                    or not isinstance(allowed, list)
                    or any(not isinstance(serial, str) or not serial for serial in allowed)
                    or len(set(allowed)) != len(allowed)
                ):
                    raise ProtocolError("MyQ guest device associations are invalid")
                schedule_ids.add(schedule_id)
                settings = {}
                for key in (
                    "schedule_type",
                    "start_date",
                    "end_date",
                    "timezone",
                    "recurring_type",
                    "recurring_value",
                    "recurring_start_time",
                    "recurring_end_time",
                ):
                    value = schedule.get(key)
                    if value is not None:
                        if not isinstance(value, str) or len(value) > 512:
                            raise ProtocolError("MyQ guest schedule details are invalid")
                        settings[key] = value
                schedules.append(GuestAccessSchedule(schedule_id, tuple(allowed), settings))
            name = item.get("guest_name")
            if not isinstance(name, str):
                name = " ".join(
                    item[k] for k in ("first_name", "last_name") if isinstance(item.get(k), str)
                )
            result.append(Guest(guest_id, name, role, tuple(schedules)))
        return tuple(result)

    async def set_guest_devices(
        self,
        device: Device,
        account_user_id: str,
        access_schedule_id: str,
        allowed_serials: list[str],
    ) -> None:
        """Edit an existing access grant without changing its temporal restrictions."""
        _identifier(account_user_id)
        _identifier(access_schedule_id)
        if (
            not isinstance(allowed_serials, list)
            or any(not isinstance(serial, str) or not serial for serial in allowed_serials)
            or len(set(allowed_serials)) != len(allowed_serials)
        ):
            raise CommandRejected("Guest access requires unique device serials")
        async with self._operation(device, account_scope=True):
            devices = await self.client.devices(device.account_id)
            if device.key not in {item.key for item in devices} or not set(
                allowed_serials
            ).issubset({item.serial for item in devices}):
                raise CommandRejected("Guest devices must belong to the current MyQ account")
            await self.require_access(device.account_id, "guests", "Manage", owner=True)
            guest = next((g for g in await self._guests(device) if g.id == account_user_id), None)
            if guest is None or guest.role not in {"guest", "guest_nomyquser"}:
                raise CommandRejected("Only an existing guest's device access can be changed")
            schedule = next((s for s in guest.schedules if s.id == access_schedule_id), None)
            if schedule is None or str(schedule.settings.get("schedule_type", "")).lower() not in {
                "always",
                "temporary",
                "recurring",
            }:
                raise CommandRejected("Guest access schedule is unavailable")
            by_serial = {item.serial: item for item in devices}
            added = set(allowed_serials) - set(schedule.allowed_devices)
            if any(not shareable(by_serial[serial]) for serial in added):
                raise CommandRejected("The MyQ app does not offer one of these devices to guests")
            payload = {**schedule.settings, "allowed_devices": list(allowed_serials)}
            await self.require_access(device.account_id, "guests", "Manage", owner=True)
            path = (
                self._guests_path(device) + f"/{_identifier(account_user_id)}/accessSchedules/"
                f"{_identifier(access_schedule_id)}"
            )
            await self.client.device_mutation(
                device, "PUT", GUESTACCESS, path, headers=NATIVE_HEADERS, json=payload
            )

            async def confirmed() -> bool:
                guest = next(
                    (g for g in await self._guests(device) if g.id == account_user_id), None
                )
                return guest is not None and any(
                    s.id == access_schedule_id
                    and set(s.allowed_devices) == set(allowed_serials)
                    and s.settings == schedule.settings
                    for s in guest.schedules
                )

            await self._confirm(confirmed)

    async def remove_guest(self, device: Device, account_user_id: str) -> None:
        _identifier(account_user_id)
        async with self._operation(device, account_scope=True):
            current = await self._current(device)
            await self.require_access(device.account_id, "guests", "Delete", owner=True)
            guest = next((g for g in await self._guests(current) if g.id == account_user_id), None)
            if guest is None or guest.role not in {"guest", "guest_nomyquser"}:
                raise CommandRejected("Only an existing guest account can be removed")
            await self.require_access(device.account_id, "guests", "Delete", owner=True)
            await self.client.device_mutation(
                current,
                "DELETE",
                GUESTACCESS,
                self._guests_path(device) + f"/{_identifier(account_user_id)}",
                headers=NATIVE_HEADERS,
            )

            async def confirmed() -> bool:
                return all(g.id != account_user_id for g in await self._guests(current))

            await self._confirm(confirmed)

    async def assign_accessory(
        self, device: Device, accessory_serial: str, account_user_id: str
    ) -> None:
        _identifier(account_user_id)
        async with self._operation(device):
            current = await self._current(device)
            if not supports_garage_settings(current):
                raise CommandRejected("Opener does not currently permit accessory management")
            accessory = await self._accessory(current, accessory_serial)
            await self.require_access(device.account_id, "guests", "Manage", owner=True)
            guest = next((g for g in await self._guests(current) if g.id == account_user_id), None)
            if guest is None or not any(
                current.serial in s.allowed_devices for s in guest.schedules
            ):
                raise CommandRejected("Accessory assignee has no confirmed access to this opener")
            access = await self.require_access(device.account_id, "devices", "Manage", owner=True)
            if not can_configure_garage(current, access):
                raise CommandRejected("Opener does not currently permit accessory management")
            route = _transmitter_route(accessory)
            await self.client.device_mutation(
                current,
                "PUT",
                GDO,
                self._accessory_path(current) + f"/{route}/assign",
                json={
                    "name": accessory.name,
                    "tx_serial_number": accessory.serial,
                    "is_delete_in_progress": False,
                    "account_user_id": account_user_id,
                },
            )

            async def confirmed() -> bool:
                return any(
                    a.serial == accessory.serial and a.account_user_id == account_user_id
                    for a in await self.accessories(current)
                )

            await self._confirm(confirmed)

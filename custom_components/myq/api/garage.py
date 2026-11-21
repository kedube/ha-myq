"""Observed residential opener settings, discovered through typed read responses.

Protocol evidence: Android wz0/a, vz0/z, xz0 DTOs, yz0/h, r80/g0,
r80/j and the native GDOLightTimerSetting enum. No generic settings endpoint.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeGuard
from urllib.parse import quote

from .account_access import AccountAccess
from .client import Client
from .const import GDO
from .errors import AccessDenied, CannotConnect, CommandRejected, ProtocolError
from .models import Device

LIGHT_TIMER_SECONDS = (90, 150, 210, 270)
_ENDPOINTS = ("lighttimer", "timetoclose", "worklight_settings")
_READ_TTL = 1800  # The app reads these only on its settings screens.
_UNSUPPORTED_TTL = 3600


def supports_garage_settings(device: Device | None) -> TypeGuard[Device]:
    """Keep native opener settings off gates, cameras and learned door sensors."""
    return (
        device is not None
        and device.family == "garagedoor"
        and device.device_type in {"garagedooropener", "wifigaragedooropener"}
    )


def can_configure_garage(
    device: Device | None, access: AccountAccess | None = None
) -> TypeGuard[Device]:
    """GdoDeviceSettingsProvider (y80/o) shows opener settings to the account owner only."""
    return (
        supports_garage_settings(device)
        and device.online
        and not device.monitor_only
        and access is not None
        and access.owner
    )


def is_3c_opener(device: Device) -> bool:
    """iv0/c.k: the gateway (or own) serial, after any "GW" prefix, starts with 3C."""
    serial = device.gateway_id or device.serial
    if len(serial) < 10:
        return False
    return serial.removeprefix("GW")[:2] == "3C"


def can_set_lock_mode(device: Device | None, access: AccountAccess | None) -> TypeGuard[Device]:
    """The Remotes & Keypads lock-mode row: owner, and a 3C opener or a Wi-Fi opener."""
    return (
        can_configure_garage(device, access)
        and (device.device_type == "wifigaragedooropener" or is_3c_opener(device))
        # iv0/b.g: null reads as false; anything else is not a lock-mode state.
        and type(device.state.get("in_vacation_mode", False)) in (bool, type(None))
    )


def _decode(endpoint: str, data: Any) -> dict[str, int | bool]:
    if not isinstance(data, dict):
        return {}
    if endpoint == "lighttimer":
        value = data.get("light_timer_setting")
        return (
            {"light_timer_setting": value}
            if type(value) is int and value in LIGHT_TIMER_SECONDS
            else {}
        )
    if endpoint == "timetoclose":
        value = data.get("time_to_close_setting")
        return (
            {"time_to_close_setting": value}
            if type(value) is int and 0 <= value <= 99 * 60 and value % 60 == 0
            else {}
        )
    pir, motion = data.get("enable_pir_control"), data.get("enable_motion_wl")
    if (
        (pir is None and motion is None)
        or (pir is not None and type(pir) is not bool)
        or (motion is not None and type(motion) is not bool)
    ):
        return {}
    # The native mapper defaults an absent sibling to false, but requires at
    # least one reported Boolean before displaying the single combined switch.
    return {"enable_pir_control": pir is True, "enable_motion_wl": motion is True}


@dataclass
class _CachedSettings:
    generation: tuple
    expires: float
    value: dict[str, int | bool]


class GarageSettings:
    """Read independently supported settings and send each mutation once."""

    def __init__(
        self,
        client: Client,
        owner_access: Callable[[str], Awaitable[AccountAccess]] | None = None,
    ) -> None:
        self.client = client
        # Re-reads the owner's grants before each write (Management.require_access).
        self.owner_access = owner_access
        self._cache: dict[tuple[str, str], _CachedSettings] = {}
        self._epochs: dict[tuple[str, str], int] = {}

    @staticmethod
    def _path(device: Device, endpoint: str) -> str:
        return (
            f"/api/v6.0/accounts/{quote(device.account_id, safe='')}/door_openers/"
            f"{quote(device.serial, safe='')}/{endpoint}"
        )

    async def _read(
        self, device: Device, endpoint: str, *, force: bool = False
    ) -> dict[str, int | bool]:
        key = device.key, endpoint
        epoch = self._epochs.get(key, 0)
        generation = (
            device.device_type,
            device.model,
            device.capabilities,
            repr(device.state.get("firmware_version")),
        )
        cached = self._cache.get(key)
        if (
            not force
            and cached is not None
            and cached.generation == generation
            and cached.expires > time.monotonic()
        ):
            return dict(cached.value)
        self._cache.pop(key, None)
        try:
            response = await self.client.request("GET", GDO, self._path(device, endpoint))
            value = _decode(endpoint, response.json())
        except ProtocolError as err:
            # These fixed status messages originate in our HTTP layer. Other
            # malformed/transient responses must not permanently disable support.
            if str(err) not in {
                "Unexpected MyQ HTTP status 404",
                "Unexpected MyQ HTTP status 405",
            }:
                raise
            value = {}
        ttl = _READ_TTL if value else _UNSUPPORTED_TTL
        # Reads started before/during a mutation cannot restore invalidated state.
        if self._epochs.get(key, 0) == epoch:
            self._cache[key] = _CachedSettings(generation, time.monotonic() + ttl, value)
        return dict(value)

    async def read(self, device: Device) -> dict[str, int | bool]:
        if not supports_garage_settings(device) or not device.online:
            return {}
        result: dict[str, int | bool] = {}
        for endpoint in _ENDPOINTS:
            try:
                result.update(await self._read(device, endpoint))
            except (AccessDenied, CannotConnect, ProtocolError):
                # An optional setting cannot take the door or sibling settings
                # offline. Authentication and rate limiting still reach the caller.
                continue
        return result

    async def _write(
        self, device: Device | None, endpoint: str, body: dict[str, int | bool]
    ) -> None:
        if device is None or self.owner_access is None:
            raise CommandRejected("Opener settings are unavailable or read only")
        if not can_configure_garage(device, await self.owner_access(device.account_id)):
            raise CommandRejected("Opener settings are unavailable or read only")
        # Re-read support/permission immediately before changing a setting.
        # A cached successful read must not authorize a later unsupported write.
        current = await self._read(device, endpoint, force=True)
        if not current:
            raise CommandRejected("This opener did not report support for the setting")
        key = device.key, endpoint
        self._epochs[key] = self._epochs.get(key, 0) + 1
        try:
            await self.client.device_mutation(
                device, "PUT", GDO, self._path(device, endpoint), json=body
            )
        finally:
            # HTTP acknowledgment does not confirm device state. The coordinator
            # obtains reported state after both successful and uncertain commands.
            self._epochs[key] += 1
            self._cache.pop(key, None)

    async def set_light_timer(self, device: Device | None, seconds: int) -> None:
        if type(seconds) is not int or seconds not in LIGHT_TIMER_SECONDS:
            raise CommandRejected("Light timer must be 90, 150, 210 or 270 seconds")
        await self._write(device, "lighttimer", {"light_timer_setting": seconds})

    async def set_time_to_close(self, device: Device | None, minutes: int) -> None:
        if type(minutes) is not int or not 0 <= minutes <= 99:
            raise CommandRejected("Time to close must be a whole number from 0 to 99 minutes")
        if device is None or not device.can_move("close"):
            raise CommandRejected("This opener does not allow unattended closing")
        await self._write(device, "timetoclose", {"time_to_close_setting": minutes * 60})

    async def set_motion_activation(self, device: Device | None, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise CommandRejected("Motion activation must be a Boolean")
        # The native app exposes one switch and always writes these together.
        await self._write(
            device,
            "worklight_settings",
            {"enable_pir_control": enabled, "enable_motion_wl": enabled},
        )

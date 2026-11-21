"""Native HVAC actions, with reported modes, units, limits and access gates."""

import math
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeGuard
from urllib.parse import quote

from .client import Client
from .const import ACTIONS, PARTNER_DEVICES
from .errors import CommandRejected, ProtocolError
from .models import Device

MODES = ("off", "heat", "cool", "heat_cool")
FAN_MODES = ("auto", "on", "circulate")
UNITS = ("celsius", "fahrenheit")


def number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _enum(value: Any) -> str | None:
    """yc1 enums use Gson's default adapter, so the app reads names like HEAT_COOL.

    Writes stay lowercase (the native mwString). Compare without regard to case.
    """
    return value.lower() if isinstance(value, str) else None


def unit(device: Device | None) -> str | None:
    if device is None:
        return None
    # HvacDevice defaults a missing or null unit to Fahrenheit; an unknown one grants
    # nothing.
    value = device.state.get("temperature_unit")
    value = "fahrenheit" if value is None else _enum(value)
    return value if value in UNITS else None


def setting(device: Device | None, key: str) -> str | None:
    """Current hvac_mode_setting / fan_mode_setting, normalized to lowercase."""
    return _enum(current(device).get(key))


def current(device: Device | None) -> dict:
    value = device.state.get("current_climate_setting") if device else None
    return value if isinstance(value, dict) else {}


def modes(device: Device | None, *, fan: bool = False) -> list[str]:
    key = "available_fan_mode_settings" if fan else "available_hvac_mode_settings"
    values = device.specifications.get(key) if device else None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return []
    reported = {_enum(value) for value in values}
    if not fan:
        # The app's on/off toggle is always shown; only heat/cool/auto follow the list.
        reported.add("off")
    return [value for value in (FAN_MODES if fan else MODES) if value in reported]


def can_control(device: Device | None) -> TypeGuard[Device]:
    return bool(
        device
        and device.is_climate
        and device.online
        and not device.monitor_only
        # GridHvacItemMapper presents !isManaged as subscription expired; HvacDevice
        # defaults a missing value to managed.
        and device.state.get("is_managed", True) is True
        and unit(device) is not None
    )


def limits(device: Device | None, kind: str) -> tuple[float, float] | None:
    if device is None or (temperature_unit := unit(device)) is None:
        return None
    low = number(device.specifications.get(f"min_{kind}_set_point_{temperature_unit}"))
    high = number(device.specifications.get(f"max_{kind}_set_point_{temperature_unit}"))
    # Setpoint bounds come from Specifications alone (HvacDevice H1). The
    # temperature_threshold is an out-of-range alert band, not a setpoint limit.
    if low is None or high is None or low > high:
        return None
    return low, high


def threshold(device: Device | None) -> dict[str, Any] | None:
    """The alert band shown on the app's Temperature Configuration screen."""
    value = device.specifications.get("temperature_threshold") if device else None
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        return None
    result: dict[str, Any] = {"enabled": value["enabled"]}
    for edge in ("lower", "upper"):
        for temperature_unit in UNITS:
            reading = number(value.get(f"{edge}_limit_{temperature_unit}"))
            if reading is not None:
                result[f"{edge}_limit_{temperature_unit}"] = reading
    return result


def target(device: Device | None, kind: str) -> float | None:
    return number(current(device).get(f"{kind}_set_point_{unit(device)}"))


# s60/HvacSetting: the minimum gap between heating and cooling targets, and the targets
# the app starts from when the thermostat reports neither.
GAP = {"celsius": Decimal(3), "fahrenheit": Decimal(5)}
DEFAULTS = {"celsius": (Decimal(20), Decimal(26)), "fahrenheit": (Decimal(68), Decimal(78))}
STEP = {"celsius": Decimal("0.1"), "fahrenheit": Decimal(1)}
NEEDED: dict[str, tuple[str, ...]] = {
    "off": (),
    "heat": ("heating",),
    "cool": ("cooling",),
    "heat_cool": ("heating", "cooling"),
}
# How long values just sent stand in for a readback that may still show the old ones.
DRAFT_SECONDS = 30.0


def _rounded(value: Any, temperature_unit: str) -> Decimal | None:
    """HvacSetting rounds before sending: whole degrees Fahrenheit, tenths Celsius, half up."""
    value = number(value)
    if value is None:
        return None
    return Decimal(repr(value)).quantize(STEP[temperature_unit], rounding=ROUND_HALF_UP)


def _filled(
    heat: Decimal | None, cool: Decimal | None, temperature_unit: str
) -> tuple[Decimal, Decimal]:
    """HvacSetting.Companion.c: derive a missing target from the other one."""
    gap = GAP[temperature_unit]
    if heat is not None and cool is not None:
        return heat, cool
    if heat is not None:
        return heat, heat + gap
    if cool is not None:
        return cool - gap, cool
    return DEFAULTS[temperature_unit]


def _within(device: Device, kind: str, value: Decimal) -> bool:
    bounds = limits(device, kind)
    return bounds is not None and bounds[0] <= float(value) <= bounds[1]


class Thermostats:
    def __init__(self, client: Client) -> None:
        self.client = client
        self._sent: dict[str, tuple[float, dict[str, Any]]] = {}

    def settings(self, device: Device) -> dict[str, Any]:
        """Mode, fan and targets to build the next request from.

        The app builds each request from its screen's draft, never from a readback that
        may lag (it waits two seconds before re-reading). Values sent in the last
        DRAFT_SECONDS override the reported ones, so a setpoint change right after a mode
        change cannot send the old mode back.
        """
        values: dict[str, Any] = {
            "mode": setting(device, "hvac_mode_setting"),
            "fan": setting(device, "fan_mode_setting"),
            "heating": target(device, "heating"),
            "cooling": target(device, "cooling"),
        }
        sent = self._sent.get(device.key)
        if sent is not None and time.monotonic() - sent[0] < DRAFT_SECONDS:
            values.update(sent[1])
        return values

    async def _send(self, device: Device, payload: dict) -> None:
        path = (
            f"/api/v1.0/accounts/{quote(device.account_id, safe='')}/devices/"
            f"{quote(device.serial, safe='')}"
        )
        await self.client.device_mutation(device, "PUT", ACTIONS, path, json=payload)

    def _targets(
        self, device: Device, draft: dict[str, Any], heating: Any, cooling: Any
    ) -> dict[str, Decimal]:
        """The app's HvacSetting rules: round, fill a missing target, keep the gap."""
        temperature_unit = unit(device)
        assert temperature_unit is not None  # can_control checked it
        gap = GAP[temperature_unit]
        for supplied in (heating, cooling):
            # Only a missing target is derived; an unusable supplied one is refused.
            if supplied is not None and _rounded(supplied, temperature_unit) is None:
                raise CommandRejected("Thermostat temperature is not a number")
        heat, cool = _filled(
            _rounded(heating if heating is not None else draft["heating"], temperature_unit),
            _rounded(cooling if cooling is not None else draft["cooling"], temperature_unit),
            temperature_unit,
        )
        if cool - heat < gap:
            # HvacSettingUpdater moves the other target when one is changed.
            if heating is not None and cooling is not None:
                raise CommandRejected("Heating and cooling targets are too close together")
            if cooling is not None:
                heat = cool - gap
            else:
                cool = heat + gap
        return {"heating": heat, "cooling": cool}

    async def _write(
        self,
        device: Device,
        mode: str,
        *,
        heating: Any = None,
        cooling: Any = None,
        fan: str | None = None,
    ) -> None:
        """One mode action carrying its targets and the fan, as HvacControlViewModel sends."""
        draft = self.settings(device)
        needed = NEEDED[mode]
        if any(
            value is not None and kind not in needed
            for kind, value in (("heating", heating), ("cooling", cooling))
        ):
            raise CommandRejected("Temperature does not apply to this thermostat mode")
        payload: dict[str, Any] = {
            "action": mode,
            "device_family": "hvacs",
            "temperature_unit": unit(device),
        }
        sent: dict[str, Any] = {"mode": mode}
        if needed:
            targets = self._targets(device, draft, heating, cooling)
            for kind in needed:
                if not _within(device, kind, targets[kind]):
                    raise CommandRejected("Thermostat temperature is outside its reported limits")
                payload[f"{kind}_set_point"] = sent[kind] = float(targets[kind])
        fan = fan or draft["fan"]
        if fan in modes(device, fan=True):
            payload["fan_mode"] = sent["fan"] = fan
        await self._send(device, payload)
        self._sent[device.key] = (time.monotonic(), sent)

    async def set_mode(
        self,
        device: Device | None,
        mode: str,
        *,
        heating: float | None = None,
        cooling: float | None = None,
    ) -> None:
        if not can_control(device) or mode not in modes(device):
            raise CommandRejected("Thermostat does not permit this mode")
        await self._write(device, mode, heating=heating, cooling=cooling)

    async def set_temperature(
        self,
        device: Device | None,
        *,
        temperature: float | None = None,
        heating: float | None = None,
        cooling: float | None = None,
        mode: str | None = None,
    ) -> None:
        """A single target applies to heat or cool; a range applies to heat_cool."""
        if not can_control(device):
            raise CommandRejected("Thermostat does not permit this mode")
        mode = mode or self.settings(device)["mode"]
        if temperature is not None:
            if mode == "heat":
                heating = temperature
            elif mode == "cool":
                cooling = temperature
            else:
                raise CommandRejected("A single temperature needs heat or cool mode")
        await self.set_mode(device, mode or "", heating=heating, cooling=cooling)

    async def set_fan(self, device: Device | None, fan: str) -> None:
        """The app changes the fan only inside the current mode's action (no set_fan_mode)."""
        if not can_control(device) or fan not in modes(device, fan=True):
            raise CommandRejected("Thermostat does not permit this fan mode")
        mode = self.settings(device)["mode"]
        if mode not in modes(device):
            raise CommandRejected("Thermostat mode is not known")
        await self._write(device, mode, fan=fan)


def _program_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return value if len(value) <= 256 else None
    return number(value)


# j01/f (presets) and j01/g (schedules): the fields the app's preset screens read.
PRESET_FIELDS = (
    "climate_preset_key",
    "name",
    "hvac_mode_setting",
    "fan_mode_setting",
    "heating_set_point_celsius",
    "heating_set_point_fahrenheit",
    "cooling_set_point_celsius",
    "cooling_set_point_fahrenheit",
    "manual_override_allowed",
)


def _rows(data: Any, key: str) -> list[dict]:
    rows = data.get(key) if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProtocolError(f"MyQ thermostat {key} response is invalid")
    return rows


def parse_presets(data: Any) -> list[dict[str, Any]]:
    return [
        {key: _program_value(row.get(key)) for key in PRESET_FIELDS if key in row}
        for row in _rows(data, "presets")
    ]


def parse_schedules(data: Any) -> list[dict[str, Any]]:
    result = []
    for row in _rows(data, "schedules"):
        days = row.get("days")
        entries = row.get("scheduled_presets")
        result.append(
            {
                "climate_schedule_key": _program_value(row.get("climate_schedule_key")),
                "name": _program_value(row.get("name")),
                # k01/e: Monday = 1 ... Sunday = 7.
                "days": [d for d in days if type(d) is int and 1 <= d <= 7]
                if isinstance(days, list)
                else [],
                "scheduled_presets": [
                    {
                        "start_time": _program_value(item.get("start_time")),
                        "climate_preset_key": _program_value(item.get("climate_preset_key")),
                    }
                    for item in entries
                    if isinstance(item, dict)
                ]
                if isinstance(entries, list)
                else [],
            }
        )
    return result


async def programs(client: Client, device: Device | None) -> dict[str, list[dict[str, Any]]]:
    """Read-only presets and schedules (myq-partner-devices v1.0, h01/a).

    The app offers these only for a managed, online thermostat. There is no working
    preset activation call in the app, so none is offered here.
    """
    if (
        device is None
        or not device.is_climate
        or not device.online
        or device.state.get("is_managed", True) is not True
    ):
        raise CommandRejected("Thermostat programs are unavailable")
    base = (
        f"/api/v1.0/accounts/{quote(device.account_id, safe='')}/devices/thermostats/"
        f"{quote(device.serial, safe='')}"
    )
    presets = parse_presets(
        (await client.request("GET", PARTNER_DEVICES, base + "/presets")).json()
    )
    schedules = parse_schedules(
        (await client.request("GET", PARTNER_DEVICES, base + "/schedules")).json()
    )
    return {"presets": presets, "schedules": schedules}

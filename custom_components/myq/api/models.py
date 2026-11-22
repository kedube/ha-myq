"""Strict decoding of observed DTO fields. Missing data remains unknown."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import DOOR_TYPES
from .errors import CommandRejected, ProtocolError


def object_list(data: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ProtocolError(f"Missing MyQ {key} list")
    rows = data[key]
    if not all(isinstance(row, dict) for row in rows):
        raise ProtocolError("Invalid MyQ list entry")
    return rows


def nested(data: Any, *keys: str) -> Any:
    """Follow dict keys; any missing or non-dict step yields None."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def section(data: Any, *keys: str) -> dict:
    """Like nested(), but always a dict so callers can chain .get()."""
    value = nested(data, *keys)
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Account:
    id: str = field(repr=False)
    name: str = field(repr=False)
    # bn0/Account created_by: the app's owner check compares it with the profile ID.
    created_by: str | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Account:
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise ProtocolError("MyQ account has no identifier")
        created_by = data.get("created_by")
        return cls(
            data["id"],
            str(data.get("name") or "MyQ"),
            created_by if isinstance(created_by, str) and created_by else None,
        )


@dataclass(frozen=True)
class Device:
    account_id: str = field(repr=False)
    serial: str = field(repr=False)
    name: str = field(repr=False)
    device_type: str
    family: str
    model: str | None
    state: dict[str, Any] = field(repr=False)
    capabilities: frozenset[str]
    specifications: dict[str, Any] = field(default_factory=dict, repr=False)
    device_platform: str = ""
    gateway_id: str | None = field(default=None, repr=False)
    parent_device_id: str | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, account_id: str, data: dict[str, Any]) -> Device:
        serial = data.get("serial_number")
        state = data.get("state")
        capabilities = data.get("capabilities")
        specifications = data.get("specifications")
        # These DTO fields are nullable in the app. Absence grants no actions.
        if state is None:
            state = {}
        if capabilities is None:
            capabilities = []
        if specifications is None:
            specifications = {}
        if (
            not isinstance(serial, str)
            or not serial
            or not isinstance(state, dict)
            or not isinstance(capabilities, list)
            or not all(isinstance(c, str) for c in capabilities)
            or not isinstance(specifications, dict)
        ):
            raise ProtocolError("Invalid MyQ device record")
        return cls(
            account_id,
            serial,
            str(data.get("name") or "MyQ device"),
            str(data.get("device_type", "unknown")).lower(),
            str(data.get("device_family", "")).lower(),
            data.get("device_model") or state.get("model_number"),
            state,
            frozenset(capabilities),
            specifications,
            str(data.get("device_platform") or "").lower(),
            gateway
            if isinstance(gateway := data.get("gateway_id") or state.get("gatewayID"), str)
            and gateway
            else None,
            parent if isinstance(parent := data.get("parent_device_id"), str) and parent else None,
        )

    @property
    def key(self) -> str:
        return f"{self.account_id}:{self.serial}"

    @property
    def online(self) -> bool:
        # LockDevice.h2: a Wi-Fi lock gateway reports its camera's connectivity
        # whenever that value is non-null.
        camera = self.state.get("camera_online")
        if self.device_type == "wifilockgateway" and camera is not None:
            return camera is True
        # Device.p1: optBoolean("online", true). Absent means online, like the app;
        # an explicit non-Boolean value is still treated as unknown connectivity.
        return self.state.get("online", True) is True

    @property
    def monitor_only(self) -> bool:
        # Device.m1 defaults to false; VirtualGarageDoorDevice.m1 defaults to true.
        # optBoolean reads null as that default; any other non-Boolean value counts
        # as monitor-only, so it grants no control.
        return self._flag("monitor_only_mode", self.device_type == "virtualgaragedooropener")

    def _flag_allows(self, key: str) -> bool:
        """An allowance flag: missing or null reads as allowed; only True otherwise."""
        value = self.state.get(key)
        return value is None or value is True

    def _flag(self, key: str, default: bool) -> bool:
        """A state Boolean as the app reads it: missing or null is the default."""
        value = self.state.get(key)
        if value is None:
            return default
        return value is not False

    def control_blocked(self) -> str | None:
        """The app's DeviceError precheck (places/view/b.java C), in its order.

        Returns why control is refused, or None. Offline is checked by callers.
        """
        if self.device_type == "virtualgaragedooropener":
            # VirtualGarageDoorDevice.v1: a learned door sensor with a fault.
            learned = self.state.get("number_of_learned_dps_devices")
            learned = 0 if learned is None else learned
            if self._flag("sensor_comm_error", False) and not (
                type(learned) is int and learned == 0
            ):
                return "The door position sensor reports a fault"
        if self.monitor_only:
            return "The device is in monitor-only mode"
        if self._flag("failed_credential_lockout_active", False):
            return "The keypad is locked out after failed PIN attempts"
        if self._flag("attached_work_light_error_present", False):
            return "The opener reports an unpaired work light"
        return None

    @property
    def is_door(self) -> bool:
        return self.device_type in DOOR_TYPES

    @property
    def is_lamp(self) -> bool:
        return self.device_type == "lamp"

    @property
    def is_climate(self) -> bool:
        return self.device_type.endswith("_thermostat") and self.family == "hvacs"

    @property
    def is_camera(self) -> bool:
        device_type = self.device_type.lower()
        return (
            self.family.lower() in {"camera", "tend"}
            # DeviceType's newer garage/indoor/keypad models do not use the
            # "-camera" suffix. Keep discovery when device_family is absent.
            or device_type
            in {
                "camera",
                "tendcamera",
                "smartlockcam",
                "smartlockcam1",
                "smartlockanzcam",
                "smartptzcam1",
                "smartflcam1",
                "sgcwlcam1",
                "videokeypadcocam",
                "videokeypadcogccam",
                "videokeypadcoanzcam",
                "smartgaragelccam2",
                "smartgaragelcanzcam2",
                "smartindoorlccam2",
                "smartindoorlcanzcam2",
            }
            or device_type.endswith("-camera")
        )

    @property
    def camera_id(self) -> str:
        # TendUtils strips the prefix up to the final hyphen from a MyQ serial.
        return self.serial.rsplit("-", 1)[-1]

    @property
    def door_state(self) -> str | None:
        state = self.state.get("door_state")
        if not isinstance(state, str):
            return None
        # GarageDoorDevice normalizes the server value before parsing its enum.
        state = state.lower()
        return state if state in {"open", "closed", "opening", "closing", "stopped"} else None

    def can_move(self, action: str) -> bool:
        return (
            self.is_door
            and self.family == "garagedoor"
            and action in {"open", "close"}
            and self.control_blocked() is None
            # Device.A1 defaults a missing or null close flag to true; the app never
            # reads the open flag. An explicit false (or malformed value) still denies.
            and self._flag_allows("is_unattended_" + action + "_allowed")
        )

    def validate_command(self, action: str) -> None:
        if not self.online:
            raise CommandRejected("Device is offline or its connectivity is unknown")
        if self.is_door and self.can_move(action):
            return
        raise CommandRejected(self.control_blocked() or "MyQ device does not permit this action")

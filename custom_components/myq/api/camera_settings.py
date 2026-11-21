"""Explicit camera settings recovered from Tend DTOs and their native mappers."""

from copy import deepcopy
from typing import Any

from .errors import CommandRejected
from .models import Device, nested
from .video import VIDEO, Video, camera_enabled

ROTATIONS = {"normal": 1, "mirror": 2, "mirror_flip": 3, "flip": 4}
NIGHT_VISION = {"off": 0, "on": 1, "auto": 2}
SIREN_LEVELS = (60, 70, 80, 90, 100)


def bounds(low: Any, high: Any, current: Any) -> tuple[int, int] | None:
    if all(type(value) is int for value in (low, high, current)) and low < high:
        return (low, high) if low <= current <= high else None
    return None


def limits(details: dict, key: str) -> tuple[int, int] | None:
    if key == "motion_sensitivity" and nested(details, "cap", "cam", "motion") is True:
        return bounds(
            nested(details, "cap", "cam", "motionSensMin"),
            nested(details, "cap", "cam", "motionSensMax"),
            nested(details, "settings", "cam", "motionSens"),
        )
    if key == "siren_volume" and nested(details, "cap", "siren", "activatable") is True:
        # The app shows siren volume only for an activatable siren, as five fixed
        # steps (xr0/DeviceSettings), within any range the camera advertises.
        current = nested(details, "settings", "siren", "vol")
        low = nested(details, "cap", "siren", "minVol")
        high = nested(details, "cap", "siren", "maxVol")
        allowed = [
            level
            for level in SIREN_LEVELS
            if (type(low) is not int or level >= low) and (type(high) is not int or level <= high)
        ]
        if type(current) is int and 0 <= current <= 100 and len(allowed) >= 2:
            return allowed[0], allowed[-1]
    return None


def siren_levels(details: dict) -> list[int]:
    window = limits(details, "siren_volume")
    return [level for level in SIREN_LEVELS if window and window[0] <= level <= window[1]]


CHIME_ALIASES = {"D": "DIGITAL", "M": "MECHANICAL", "N": "NONE"}
# hb0/m: the app's chime-type flow offers these two, each with its own duration.
CHIME_TYPES = {"mechanical": ("MECHANICAL", 0), "digital": ("DIGITAL", 5)}
POWER_SAVER = ("AUTO", "ON", "OFF")


def _power_saver_modes(details: dict) -> list[str]:
    """cap.pwr.powerSaverModes: Gson enum names, so exact uppercase AUTO/ON/OFF."""
    modes = nested(details, "cap", "pwr", "powerSaverModes")
    if not isinstance(modes, list):
        return []
    return [mode for mode in POWER_SAVER if mode in modes]


def choices(details: dict, key: str) -> dict[str, Any]:
    if key == "power_saver":
        modes = _power_saver_modes(details)
        return {mode.lower(): mode for mode in modes} if len(modes) >= 2 else {}
    if key == "chime_type":
        types = nested(details, "cap", "chime", "typeList")
        if not isinstance(types, list):
            return {}
        advertised = {CHIME_ALIASES.get(item, item) for item in types if isinstance(item, str)}
        return {name: wire for name, (wire, _) in CHIME_TYPES.items() if wire in advertised}
    if key == "rotation":
        current = nested(details, "settings", "cam", "rotation")
        advertised = nested(details, "cap", "cam", "rotationList")
        if not isinstance(advertised, list) or not all(type(v) is int for v in advertised):
            return {}
        if type(current) is int and current in ROTATIONS.values():
            return {name: value for name, value in ROTATIONS.items() if value in advertised}
        if isinstance(current, str) and current.lower() in ROTATIONS:
            return {name: name.upper() for name, value in ROTATIONS.items() if value in advertised}
    if key == "night_vision" and nested(details, "cap", "cam", "ir") is True:
        current = nested(details, "settings", "cam", "ir")
        if type(current) is int and current in NIGHT_VISION.values():
            return NIGHT_VISION.copy()
        if isinstance(current, str) and current.lower() in NIGHT_VISION:
            return {name: name.upper() for name in NIGHT_VISION}
    return {}


def value(details: dict, key: str) -> Any:
    paths = {
        "motion_sensitivity": ("cam", "motionSens"),
        "siren_volume": ("siren", "vol"),
        "rotation": ("cam", "rotation"),
        "night_vision": ("cam", "ir"),
        "status_led": ("statusLed", "status"),
        "chime_enabled": ("chime", "enabled"),
        "auto_tracking": ("cam", "ptz", "autoTracking", "enabled"),
        "wake_on_motion": ("wakeOnMotion",),
        "power_saver": ("pwr", "powerSaverMode"),
        "chime_type": ("chime", "type"),
    }
    if key not in paths:
        return None
    raw = nested(details, "settings", *paths[key])
    if key == "chime_type":
        raw = CHIME_ALIASES.get(raw, raw)
    if key in {"rotation", "night_vision", "power_saver", "chime_type"}:
        return next((name for name, item in choices(details, key).items() if item == raw), None)
    if key == "status_led":
        # i41/p0 compares with equalsIgnoreCase("ON").
        return {"ON": True, "OFF": False}.get(raw.upper()) if isinstance(raw, str) else None
    return raw


def supported(details: dict, key: str) -> bool:
    if key in {"motion_sensitivity", "siren_volume"}:
        return limits(details, key) is not None
    if key in {"rotation", "night_vision", "power_saver"}:
        return bool(choices(details, key)) and value(details, key) is not None
    if key == "chime_type":
        chime = nested(details, "settings", "chime")
        if not isinstance(chime, dict) or not isinstance(chime.get("type"), str):
            return False
        current = CHIME_ALIASES.get(chime["type"], chime["type"])
        return (
            bool(choices(details, key))
            and type(chime.get("enabled")) is bool
            and current in CHIME_ALIASES.values()
        )
    if key == "wake_on_motion":
        # i41/e1: a null value is NOT_COMPATIBLE. Where power saver modes exist, the
        # app manages power with that selection instead (ie0/d0).
        return (
            nested(details, "cap", "wakeOnMotionCap") is True
            and type(value(details, key)) is bool
            and not _power_saver_modes(details)
        )
    if key == "status_led":
        return (
            nested(details, "cap", "statusLed", "ledOnOff") is True
            and type(value(details, key)) is bool
        )
    if key == "chime_enabled":
        # Native ChimeSettingsPayload preserves type and duration with enabled.
        types = nested(details, "cap", "chime", "typeList")
        current_type = nested(details, "settings", "chime", "type")
        aliases = CHIME_ALIASES
        return (
            isinstance(types, list)
            and bool(types)
            and all(isinstance(item, str) for item in types)
            and isinstance(current_type, str)
            and aliases.get(current_type, current_type)
            in [aliases.get(item, item) for item in types]
            and type(value(details, key)) is bool
            and current_type in ("D", "M", "N", "DIGITAL", "MECHANICAL", "NONE")
            and type(nested(details, "settings", "chime", "duration")) is int
            and nested(details, "settings", "chime", "duration") >= 0
        )
    if key == "auto_tracking":
        objects = nested(details, "settings", "cam", "ptz", "autoTracking", "objects")
        return (
            nested(details, "cap", "cam", "ptz", "autoTracking", "enabled") is True
            and type(value(details, key)) is bool
            and isinstance(objects, list)
            and all(isinstance(item, str) and item for item in objects)
        )
    return False


def lights(details: dict) -> list[tuple[dict, dict]]:
    """Preserve positional light-list semantics; malformed entries grant no control."""
    caps, settings = nested(details, "cap", "lightList"), nested(details, "settings", "lightList")
    if (
        not isinstance(caps, list)
        or not isinstance(settings, list)
        or not 1 <= len(caps) == len(settings) <= 16
    ):
        return []
    for cap, setting in zip(caps, settings, strict=True):
        if not isinstance(cap, dict) or not isinstance(setting, dict):
            return []
        states = cap.get("switchStateList")
        if (
            not isinstance(states, list)
            or not all(isinstance(s, str) for s in states)
            or not {"on", "off"}.issubset(states)
            or setting.get("switchState") not in ("on", "off", "program")
            or type(setting.get("intensity")) is not int
            or type(setting.get("motionTrigger")) is not bool
            or not isinstance(setting.get("triggerDuration"), str)
        ):
            return []
    return list(zip(caps, settings, strict=True))


def light_limits(cap: dict, setting: dict) -> tuple[int, int] | None:
    if cap.get("setIntensity") is True:
        return bounds(cap.get("intensityMin"), cap.get("intensityMax"), setting.get("intensity"))
    return None


class CameraSettings:
    def __init__(self, video: Video) -> None:
        self.video = video

    async def _details(self, device: Device | None) -> dict:
        if device is None or not device.is_camera or not device.online:
            raise CommandRejected("Camera is unavailable")
        # Wakes a sleeping camera first; rejects one whose connectivity is down.
        return await self.video.ready(device)

    async def set(self, device: Device | None, key: str, desired: Any) -> None:
        if device is None:
            raise CommandRejected("Camera is no longer registered")
        # Share Video's lock with existing privacy/microphone/settings operations.
        async with self.video.settings_operation(device):
            details = await self._details(device)
            if not supported(details, key):
                raise CommandRejected("The camera does not advertise this setting")
            payload: dict
            if key in {"motion_sensitivity", "siren_volume"}:
                bounds = limits(details, key)
                if (
                    bounds is None
                    or type(desired) is not int
                    or not bounds[0] <= desired <= bounds[1]
                    or (key == "siren_volume" and desired not in siren_levels(details))
                ):
                    raise CommandRejected("Camera setting is outside its advertised range")
                section, field = (
                    ("cam", "motionSens") if key == "motion_sensitivity" else ("siren", "vol")
                )
                payload = {section: {field: desired}}
            elif key in {"rotation", "night_vision"}:
                options = choices(details, key)
                if not isinstance(desired, str) or desired not in options:
                    raise CommandRejected("Unsupported camera setting option")
                payload = {"cam": {"rotation" if key == "rotation" else "ir": options[desired]}}
            elif key == "power_saver":
                options = choices(details, key)
                if not isinstance(desired, str) or desired not in options:
                    raise CommandRejected("Unsupported power saver mode")
                payload = {"pwr": {"powerSaverMode": options[desired]}}
            elif key == "chime_type":
                if not isinstance(desired, str) or desired not in choices(details, key):
                    raise CommandRejected("Unsupported chime type")
                wire, duration = CHIME_TYPES[desired]
                # ChimeSettingsPayload always carries type, duration and enabled.
                enabled = nested(details, "settings", "chime", "enabled")
                payload = {"chime": {"enabled": enabled, "type": wire, "duration": duration}}
            else:
                if type(desired) is not bool:
                    raise CommandRejected("Camera setting must be enabled or disabled")
                if key == "status_led":
                    payload = {"statusLed": {"status": "ON" if desired else "OFF"}}
                elif key == "wake_on_motion":
                    payload = {"wakeOnMotion": desired}
                elif key == "chime_enabled":
                    old = nested(details, "settings", "chime")
                    payload = {
                        "chime": {
                            "enabled": desired,
                            "type": old["type"],
                            "duration": old["duration"],
                        }
                    }
                else:
                    if not camera_enabled(details):
                        raise CommandRejected("PTZ auto tracking requires an enabled camera")
                    payload = {
                        "cam": {
                            "ptz": {
                                "autoTracking": {
                                    "enabled": desired,
                                    "objects": nested(
                                        details, "settings", "cam", "ptz", "autoTracking", "objects"
                                    ),
                                }
                            }
                        }
                    }
            await self._put(device, payload)

    async def light(
        self, device: Device | None, index: int, enabled: bool, brightness: int | None = None
    ) -> None:
        if device is None:
            raise CommandRejected("Camera is no longer registered")
        async with self.video.settings_operation(device):
            details = await self._details(device)
            items = lights(details)
            if type(index) is not int or not 0 <= index < len(items) or type(enabled) is not bool:
                raise CommandRejected("Camera light is unavailable")
            cap, setting = items[index]
            # Native LightSettingsMapper emits these four fields for every light.
            payload = [
                {
                    field: deepcopy(old[field])
                    for field in ("switchState", "intensity", "motionTrigger", "triggerDuration")
                }
                for _, old in items
            ]
            payload[index]["switchState"] = "on" if enabled else "off"
            if brightness is not None:
                limits = light_limits(cap, setting)
                if type(brightness) is not int or not 1 <= brightness <= 255 or limits is None:
                    raise CommandRejected("Camera light brightness is unavailable")
                low, high = limits
                payload[index]["intensity"] = round(low + (high - low) * (brightness - 1) / 254)
            await self._put(device, {"lightList": payload})

    async def _put(self, device: Device, payload: dict) -> None:
        await self.video.client.device_mutation(
            device,
            "PUT",
            VIDEO,
            self.video._path(device) + "/settings",
            headers={"AntiCSRF": "AntiCSRF"},
            json=payload,
        )

"""Tend camera details, settings, and refreshed snapshots from the APK."""

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .client import Client
from .errors import (
    AuthenticationError,
    CannotConnect,
    CommandRejected,
    MyQError,
    ProtocolError,
    RateLimited,
    UnsupportedAuthStep,
)
from .exclusive import ExclusiveLocks
from .models import Device, nested

VIDEO = "https://media.hubs.tend-us.tendplatform.com"
# Live view: DevicesManager waits for a push with one fallback read after 30 s.
# Without that push here, details are re-read every 3 s (at most 10 reads).
WAKEUP_TIMEOUT = 30
WAKEUP_POLL_INTERVAL = 3
# Settings writes: RestApiVideoKeypadDeviceAwakener (mz0/b) polls every 2 s for 15 s.
SETTINGS_WAKE_TIMEOUT = 15
SETTINGS_WAKE_INTERVAL = 2
# CameraSnapshotProvider's image requests time out after 6 s (hj0/m, Glide OkHttp).
SNAPSHOT_TIMEOUT = 6
# A battery camera's existing image is refreshed once it is this old (wy/o 418-436).
STALE_IMAGE = timedelta(hours=24)
# TendDevice.checkRequiresWakeup(), qf1/c model values. The model is the
# second colon-delimited component of hw.platform, not the display hw.model.
WAKEUP_MODELS = frozenset(
    {
        "GT-RTL8735BDM-II",
        "GT-RTL8735BDM-III",
        "PMX-SAV524MT7686-STM-I",
        "PMX-SAV524MT7686-STM-IV",
        "PMX-SAV524MT7686-STM-II",
        "PMX-SAV524MT7686-STM-III",
        "KDS-SAV524MT7686-CYPHS-I",
        "KDS-SAV524MT7686-CYPHS-II",
    }
)
# VideoDeviceInfo.n() (f117308k) adds the CO video keypads to the same list.
SLEEPING_MODELS = WAKEUP_MODELS | {
    "PMX-RTL8735BM-AL4-VII",
    "PMX-RTL8735BM-AL4-VIII",
    "PMX-RTL8735BM-AL4-IX",
}


def _power(details: dict) -> tuple[dict, dict | None]:
    status = details.get("status")
    if status is None:
        status = {}
    if not isinstance(status, dict):
        raise ProtocolError("MyQ camera status is invalid")
    power = status.get("pwr")
    if power is not None and not isinstance(power, dict):
        raise ProtocolError("MyQ camera power status is invalid")
    return status, power


def camera_online(details: dict) -> bool:
    """i41/l reads a missing or non-Boolean status.online as offline."""
    status = details.get("status")
    return isinstance(status, dict) and status.get("online") is True


def sleep_mode(details: dict) -> str | None:
    """SleepModeDeserializer: case-insensitive; any other value is UNKNOWN."""
    mode = nested(details, "status", "pwr", "sleepMode")
    if mode is None:
        return None
    mode = mode.upper() if isinstance(mode, str) else "UNKNOWN"
    return mode if mode in ("FULL_POWER", "LOW_POWER_DEEP_SLEEP", "FULL_POWER_BOOT") else "UNKNOWN"


def camera_enabled(details: dict) -> bool:
    """Not in privacy mode, as the app reads it.

    CameraSettings.enabled defaults to true when absent (i41/c:101), and privacy is
    enableSwitch && !enabled (ue1/g.c). A malformed value is treated as off.
    """
    enabled = nested(details, "settings", "cam", "enabled")
    if enabled is None:
        return True
    if type(enabled) is not bool:
        return False
    return enabled or nested(details, "cap", "cam", "enableSwitch") is not True


def captured_at(value: str | None) -> datetime | None:
    """The tend-dttm image header: yyyy-MM-dd'T'HH:mm:ss UTC, prefix-parsed (d0.i)."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _sleep_interval(power: dict | None) -> bool:
    interval = power.get("sleepInterval") if power is not None else None
    if interval is None:
        return False
    if type(interval) not in (int, float) or not math.isfinite(interval):
        raise ProtocolError("MyQ camera sleep interval is invalid")
    return interval > 0


def _model(details: dict) -> str | None:
    hardware = details.get("hw")
    if hardware is None:
        return None
    if not isinstance(hardware, dict):
        raise ProtocolError("MyQ camera hardware information is invalid")
    platform = hardware.get("platform")
    if platform is None:
        return None
    if not isinstance(platform, str):
        raise ProtocolError("MyQ camera hardware platform is invalid")
    # Java StringTokenizer ignores empty components.
    parts = [part for part in platform.split(":") if part]
    return parts[1].strip() if len(parts) == 2 else None


def _requires_wakeup(details: dict) -> bool:
    """TendDevice.checkRequiresWakeup, used before live view."""
    _, power = _power(details)
    return _sleep_interval(power) or _model(details) in WAKEUP_MODELS


def _sleeps(details: dict) -> bool:
    """VideoDeviceInfo.n(): power data decides when present, else the hardware model."""
    _, power = _power(details)
    if power is not None:
        return _sleep_interval(power)
    return _model(details) in SLEEPING_MODELS


def _fatal(error: MyQError) -> bool:
    """Errors a snapshot fallback must not hide."""
    return isinstance(error, (AuthenticationError, RateLimited, UnsupportedAuthStep))


def battery_level(details: dict) -> int | None:
    level = nested(details, "status", "pwr", "battLv")
    return level if type(level) is int and 0 <= level <= 100 else None


def battery_powered(details: dict) -> bool:
    """k80/a.a: a camera that reports a valid battery level runs on its battery."""
    return battery_level(details) is not None


def battery_healthy(details: dict) -> bool:
    """k80/a -> wy/o: above the CRITICAL_LOW band (6-10 %) and wake on motion not off.

    The snapshot decision reads only DeviceSettings.n() (wakeOnMotion disabled), not
    the power saver mode shown on the Battery screen.
    """
    level = battery_level(details)
    return (
        level is not None
        and level > 10
        and nested(details, "settings", "wakeOnMotion") is not False
    )


def _awake(details: dict, requires_wakeup: bool) -> bool:
    """VideoDeviceInfo.c(): without a sleep mode, AVAILABLE unless it sleeps.

    An unrecognized sleep mode is UNKNOWN, which is not available.
    """
    _power(details)
    if not camera_online(details):
        return False
    mode = sleep_mode(details)
    if mode is None:
        return not requires_wakeup
    return mode == "FULL_POWER"


def wakeable(details: dict) -> bool:
    """A camera that may report offline while asleep, so a wake is worth sending."""
    try:
        return _requires_wakeup(details)
    except ProtocolError:
        return False


class Video:
    def __init__(self, client: Client) -> None:
        self.client = client
        self._locks = ExclusiveLocks()
        # Capture time (tend-dttm) of the latest image returned for each camera.
        self.captured: dict[str, datetime | None] = {}

    @asynccontextmanager
    async def settings_operation(self, device: Device | None):
        """Reject overlapping read/modify/write operations instead of queuing them."""
        if device is None or not device.is_camera or not device.online:
            raise CommandRejected("Camera is unavailable")
        async with self._locks.hold(device.key, "A camera settings change is already in progress"):
            yield

    @staticmethod
    def _path(device: Device | None) -> str:
        if device is None or not device.is_camera or not device.camera_id:
            raise CommandRejected("This device is not a camera")
        return "/cxs/api/devices/" + quote(device.camera_id, safe="")

    async def details(self, device: Device) -> dict[str, Any]:
        response = await self.client.request(
            "GET",
            VIDEO,
            self._path(device),
            headers={"AntiCSRF": "AntiCSRF"},
        )
        data = response.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("settings"), dict)
            or not isinstance(data.get("cap"), dict)
        ):
            raise ProtocolError("Invalid MyQ camera details")
        for group in ("settings", "cap"):
            for section in ("cam", "mic", "spk"):
                value = data[group].get(section)
                if value is None:
                    data[group].pop(section, None)
                elif not isinstance(value, dict):
                    raise ProtocolError("Invalid MyQ camera settings or capabilities")
        return data

    async def _post_wakeup(self, device: Device) -> None:
        await self.client.request(
            "POST",
            VIDEO,
            self._path(device) + "/do/wakeup",
            headers={"AntiCSRF": "AntiCSRF"},
            json={},
        )

    async def _wait_awake(
        self, device: Device, *, live: bool, limit: float, interval: float
    ) -> dict[str, Any]:
        async with asyncio.timeout(limit):
            while True:
                details = await self.details(device)
                if _awake(details, True if live else _sleeps(details)):
                    return details
                await asyncio.sleep(interval)

    async def wakeup(
        self, device: Device, details: dict | None = None, *, force: bool = False
    ) -> dict:
        """Check availability before live view, waking a camera that sleeps.

        TendDevice.checkRequiresWakeup decides; DevicesManager sends the POST even when
        the camera reports offline, then waits. Android waits for an SDK push with a
        30-second fallback read; this integration re-reads every few seconds within
        the same bound. The POST is sent once.
        """
        self._path(device)
        if details is None:
            details = await self.details(device)
        # An unrecognized power mode reads as UNKNOWN, not available: try a wake.
        unknown = camera_online(details) and sleep_mode(details) == "UNKNOWN"
        required = force or unknown or _requires_wakeup(details)
        if not required:
            if not _awake(details, False):
                raise CommandRejected("MyQ camera is unavailable for live video")
            return details
        await self._post_wakeup(device)
        try:
            return await self._wait_awake(
                device, live=True, limit=WAKEUP_TIMEOUT, interval=WAKEUP_POLL_INTERVAL
            )
        except TimeoutError:
            raise CannotConnect("MyQ camera did not wake up in time") from None

    async def ready(self, device: Device) -> dict[str, Any]:
        """Fresh details for a settings write, waking a camera that is not AVAILABLE.

        mz0/c: settings requests default to requiresHardwareDeviceOnline, and a camera
        whose VideoDeviceInfo state is not AVAILABLE is woken first (mz0/b polls every
        2 s for 15 s). The app's handling of a failed wake did not decompile; here the
        write proceeds if the camera still reports online, and is refused otherwise.
        """
        details = await self.details(device)
        if not camera_online(details) and not wakeable(details):
            raise CommandRejected("Camera connectivity is unavailable")
        if _awake(details, _sleeps(details)):
            return details
        await self._post_wakeup(device)
        try:
            return await self._wait_awake(
                device, live=False, limit=SETTINGS_WAKE_TIMEOUT, interval=SETTINGS_WAKE_INTERVAL
            )
        except TimeoutError:
            details = await self.details(device)
            if camera_online(details):
                return details
            raise CannotConnect("MyQ camera did not wake up in time") from None

    async def _image(self, device: Device, mode: str) -> tuple[bytes, datetime | None]:
        response = await self.client.request(
            "GET",
            VIDEO,
            self._path(device) + "/cam/recentImage.jpg",
            headers={"AntiCSRF": "AntiCSRF"},
            params={"size": "HD", mode: "true"},
            timeout=SNAPSHOT_TIMEOUT,
        )
        if not response.body.startswith(b"\xff\xd8\xff"):
            raise ProtocolError("MyQ camera did not return a JPEG")
        # TendSnapshotInterceptor (hj0/c0) records the capture time from this header.
        return response.body, captured_at(response.header("tend-dttm"))

    async def snapshot(self, device: Device, details: dict | None = None) -> bytes:
        """CameraSnapshotProvider (wy/o) mode choice, with its capture-time rule.

        A battery camera asks for its existing image. It forces a fresh one when that
        fails, or when the existing image is missing a capture time or is at least
        24 hours old, but only while its battery is healthy. Other cameras refresh and
        fall back to the existing image. Any load failure falls back, as in the app;
        authentication, rate limits and browser verification still surface.
        """
        if details is not None and battery_powered(details):
            healthy = battery_healthy(details)
            try:
                body, taken = await self._image(device, "forceUseExisting")
            except MyQError as err:
                if _fatal(err) or not healthy:
                    raise
                body, taken = await self._image(device, "forceRefresh")
            else:
                if healthy and (taken is None or datetime.now(UTC) - taken >= STALE_IMAGE):
                    try:
                        body, taken = await self._image(device, "forceRefresh")
                    except MyQError as err:
                        if _fatal(err):
                            raise
        else:
            try:
                body, taken = await self._image(device, "forceRefresh")
            except MyQError as err:
                if _fatal(err):
                    raise
                body, taken = await self._image(device, "forceUseExisting")
        self.captured[device.key] = taken
        return body

    async def setting(self, device: Device | None, section: str, key: str, value: Any) -> None:
        """Read current capability/setting before sending a minimal update."""
        if device is None:
            raise CommandRejected("Camera is no longer registered")
        async with self.settings_operation(device):
            details = await self.ready(device)
            settings = details["settings"].get(section, {})
            capabilities = details["cap"].get(section, {})
            if (
                not device.online
                or not camera_online(details)
                or not isinstance(settings, dict)
                or key not in settings
                or not isinstance(capabilities, dict)
                or section not in details["cap"]
            ):
                raise CommandRejected("Camera setting is unavailable")
            allowed = False
            if section == "cam" and key == "enabled":
                allowed = (
                    capabilities.get("enableSwitch") is True
                    and type(value) is bool
                    and type(settings[key]) is bool
                )
            elif section == "cam" and key == "motionEnabled":
                allowed = (
                    capabilities.get("motion") is True
                    and type(value) is bool
                    and type(settings[key]) is bool
                )
            elif section == "mic" and key == "enabled":
                allowed = type(value) is bool and type(settings[key]) is bool
            elif section == "spk" and key == "vol" and type(value) is int:
                low, high = capabilities.get("volMin"), capabilities.get("volMax")
                allowed = (
                    type(low) is int
                    and type(high) is int
                    and type(settings[key]) is int
                    and low <= value <= high
                )
            if not allowed:
                raise CommandRejected("The camera does not advertise support for this setting")
            await self.client.device_mutation(
                device,
                "PUT",
                VIDEO,
                self._path(device) + "/settings",
                headers={"AntiCSRF": "AntiCSRF"},
                json={section: {key: value}},
            )

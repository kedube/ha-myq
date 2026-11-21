"""Typed, bounded controls for an already authenticated camera live session.

Wire values come from g81/c, k81/a, rd0/e and l81/c,d in Android 5.324.
Local timers and locks are integration safeguards, not camera acknowledgements.
"""

import asyncio
from contextlib import suppress
from copy import deepcopy

from aiortc import MediaStreamTrack
from av import AudioFrame

from .errors import CommandRejected, MyQError
from .models import section
from .video import camera_enabled

PTZ_ACTIONS = {
    "left": ("P", "-1"),
    "right": ("P", "1"),
    "down": ("T", "-1"),
    "up": ("T", "1"),
    "zoom_in": ("Z", "1"),
    "zoom_out": ("Z", "0"),
    "stop": ("S", "0"),
}
MAX_TALKBACK_SECONDS = 30
MAX_SIREN_SECONDS = 60
PTZ_STOP_SECONDS = 1


def require_control(details: dict, feature: str, *, permitted: bool) -> None:
    """Pure capability and readiness gate, shared by live sessions and button availability."""
    if permitted is not True:
        raise CommandRejected("Camera control is not permitted")
    status = section(details, "status")
    if (
        status.get("online") is not True
        or status.get("upgradeInProgress") is True
        or not camera_enabled(details)
    ):
        raise CommandRejected("Camera is unavailable, updating, or in privacy mode")
    if feature == "ptz":
        if not isinstance(section(details, "cap", "cam").get("ptz"), dict):
            raise CommandRejected("Camera does not advertise pan/tilt controls")
        camera_status = status.get("cam")
        if camera_status is not None and not isinstance(camera_status, dict):
            raise CommandRejected("Camera pan/tilt status is invalid")
        ptz_status = section(status, "cam").get("ptz")
        if ptz_status is not None and not isinstance(ptz_status, dict):
            raise CommandRejected("Camera pan/tilt status is invalid")
        calibration = section(status, "cam", "ptz").get("calibrationResult")
        if calibration not in (None, "success"):
            raise CommandRejected("Calibrate this camera in the MyQ app before moving it")
        # px0/k maps homing, privacy and error separately from normal movement.
        state = section(status, "cam", "ptz").get("state")
        if state not in (None, "idle", "manual", "patrol", "tracking"):
            raise CommandRejected("Camera pan/tilt is not ready for manual movement")
    elif feature == "siren":
        if section(details, "cap", "siren").get("activatable") is not True:
            raise CommandRejected("Camera does not advertise a controllable siren")
    elif feature == "talkback":
        if section(details, "cap", "spk").get("talkback") is not True:
            raise CommandRejected("Camera does not advertise two-way audio")


class MutedMicrophone(MediaStreamTrack):
    """Drain the browser track continuously; forward silence until explicitly enabled."""

    kind = "audio"

    def __init__(self, source: MediaStreamTrack) -> None:
        super().__init__()
        self.source = source
        self.enabled = False
        self._timer: asyncio.TimerHandle | None = None

    def enable(self, enabled: bool) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.enabled = enabled
        if enabled:
            self._timer = asyncio.get_running_loop().call_later(
                MAX_TALKBACK_SECONDS, self.enable, False
            )

    async def recv(self):
        frame = await self.source.recv()
        if self.enabled:
            return frame
        silent = AudioFrame(
            format=frame.format.name, layout=frame.layout.name, samples=frame.samples
        )
        for plane in silent.planes:
            plane.update(bytes(plane.buffer_size))
        silent.sample_rate, silent.pts, silent.time_base = (
            frame.sample_rate,
            frame.pts,
            frame.time_base,
        )
        return silent

    def stop(self) -> None:
        self.enable(False)
        self.source.stop()
        super().stop()


class LiveControls:
    """No queue or replay; stop requests remain possible after permission revocation."""

    def __init__(self, tend) -> None:
        self.tend = tend
        self.details: dict = {}
        self.permitted = False
        self._lock = asyncio.Lock()
        self._closed = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._ptz_active = False
        self._siren_active = False
        self._timers: dict[str, asyncio.Task] = {}

    def configure(self, details: dict, *, can_control: bool) -> None:
        self.details = deepcopy(details)
        self.permitted = can_control is True

    def require(self, feature: str) -> None:
        if self._closed:
            raise CommandRejected("Camera control is not permitted")
        require_control(self.details, feature, permitted=self.permitted)

    def _schedule_stop(self, name: str, seconds: float) -> None:
        previous = self._timers.pop(name, None)
        if previous is not None:
            previous.cancel()

        async def stop_later() -> None:
            try:
                await asyncio.sleep(seconds)
                if name == "ptz":
                    await self.ptz("stop")
                else:
                    await self.siren(False)
            except MyQError:
                # The Tend transport closes on a failed write. Never replay ON/move.
                pass
            finally:
                if self._timers.get(name) is asyncio.current_task():
                    self._timers.pop(name, None)

        self._timers[name] = asyncio.create_task(stop_later())

    def _cancel_stop(self, name: str) -> None:
        task = self._timers.pop(name, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def ptz(self, action: str) -> None:
        if action not in PTZ_ACTIONS:
            raise CommandRejected("Unsupported camera movement")
        if action != "stop":
            self.require("ptz")
            if self._lock.locked() or self._ptz_active:
                raise CommandRejected("Camera movement is already in progress")
        async with self._lock:
            if action != "stop":
                self.require("ptz")
                self._ptz_active = True
                # A finite native step plus a watchdog stop; never send C/-C.
                self._schedule_stop("ptz", PTZ_STOP_SECONDS)
            await self.tend.control("set-ptz", (PTZ_ACTIONS[action],))
            if action == "stop":
                self._ptz_active = False
                self._cancel_stop("ptz")

    async def siren(self, enabled: bool, *, duration: int = 10) -> None:
        if type(enabled) is not bool:
            raise CommandRejected("Invalid siren action")
        if enabled:
            self.require("siren")
            if type(duration) is not int or not 1 <= duration <= MAX_SIREN_SECONDS:
                raise CommandRejected("Siren duration must be 1 to 60 seconds")
            if self._lock.locked() or self._siren_active:
                raise CommandRejected("A camera command is already in progress")
        async with self._lock:
            if enabled:
                self.require("siren")
                self._siren_active = True
                self._schedule_stop("siren", duration)
            await self.tend.control("trigger-siren-action", (("31", "01" if enabled else "02"),))
            if not enabled:
                self._siren_active = False
                self._cancel_stop("siren")

    async def close(self) -> None:
        """Finish owned stop commands even if a caller abandons shutdown."""
        if self._cleanup_task is None:
            self._closed = True
            self._cleanup_task = asyncio.create_task(self._close_impl())
        await asyncio.shield(self._cleanup_task)

    async def _close_impl(self) -> None:
        tasks, self._timers = list(self._timers.values()), {}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Finish owned actuation before Tend logs out, even if permission changed.
        # An in-flight send must finish before cleanup sends its stop commands.
        async with self._lock:
            for active, command, actions in (
                (self._ptz_active, "set-ptz", (("S", "0"),)),
                (self._siren_active, "trigger-siren-action", (("31", "02"),)),
            ):
                if active:
                    with suppress(MyQError, OSError, TimeoutError):
                        async with asyncio.timeout(2):
                            await self.tend.control(command, actions, cleanup=True)
            self._ptz_active = self._siren_active = False

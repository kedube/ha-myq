"""Siren and saved PTZ positions without an open live view.

The app sends these over its always-on Tend session (Audio Settings siren test,
r80/f; preset moves, rd0/e GOTO "G"). The integration prefers an open live view's
session (a WebRTC view's, or a TC camera's video session), then the idle status
session, then a short login used only for the command.
Nothing is queued or replayed; a timed siren always ends with an explicit stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .client import Client
from .errors import CommandRejected, MyQError
from .live_controls import MAX_SIREN_SECONDS, require_control
from .models import Device, nested
from .status import CameraStatusMonitor
from .tend import TendCommandSession, _alias

SIREN_ON = (("31", "01"),)
SIREN_OFF = (("31", "02"),)


def presets(details: dict) -> list[tuple[int, str]]:
    """Saved PTZ positions (PresetDto id, index, name), as (id, display name)."""
    raw = nested(details, "settings", "cam", "ptz", "presets")
    if not isinstance(raw, list) or nested(details, "settings", "cam", "ptz", "presetDrifted"):
        return []
    result, seen = [], set()
    for item in raw[:64]:
        if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 0:
            continue
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        name = item.get("name")
        label = name.strip()[:40] if isinstance(name, str) and name.strip() else None
        result.append((item["id"], label or f"Preset {item['id']}"))
    return result


class CameraCommands:
    def __init__(
        self,
        client: Client,
        monitor: CameraStatusMonitor,
        live: Callable[[str], Any],
        video: Callable[[str], Any] = lambda key: None,
    ) -> None:
        self.client = client
        self.monitor = monitor
        self._live = live
        # A TC camera's open video session, if any (device key -> session).
        self._video = video
        self._sirens: dict[str, tuple[asyncio.Task, dict[str, bool], Device, dict]] = {}

    async def _send(
        self, device: Device, details: dict, command: str, actions: tuple[tuple[str, str], ...]
    ) -> None:
        alias = _alias(details.get("alias"))
        stream = self._live(device.key)
        if stream is not None and getattr(stream, "ready", False):
            await stream.send_command(command, actions)
            return
        video = self._video(device.key)
        if video is not None and getattr(video, "connected", False):
            # The status session is paused while video plays, and another login
            # beside the video session could end it: use the one already open.
            await video.send_to(alias, command, actions)
            return
        if await self.monitor.command(alias, command, actions):
            return
        session = TendCommandSession()
        try:
            await session.open(await self.client.auth.access_token())
            await session.send_to(alias, command, actions)
        finally:
            await session.close()

    async def siren(self, device: Device, details: dict, *, seconds: int = 10) -> None:
        require_control(details, "siren", permitted=True)
        if type(seconds) is not int or not 1 <= seconds <= MAX_SIREN_SECONDS:
            raise CommandRejected("Siren duration must be 1 to 60 seconds")
        _alias(details.get("alias"))  # Refuse before touching a stop that is armed.
        await self._cancel_timer(device.key)
        owned = {"stop": True}

        async def stop_later() -> None:
            try:
                await asyncio.sleep(seconds)
            finally:
                # The timer sends the stop unless stop_siren() or close() took it over.
                if owned["stop"]:
                    await self._stop_quietly(device, details)
                if self._sirens.get(device.key, (None,))[0] is asyncio.current_task():
                    self._sirens.pop(device.key, None)

        # The stop is armed before the siren is asked to sound. If that request fails
        # with its outcome unknown, or replaces a siren already sounding, the stop
        # still goes out.
        self._sirens[device.key] = (asyncio.create_task(stop_later()), owned, device, details)
        await self._send(device, details, "trigger-siren-action", SIREN_ON)

    async def _stop_quietly(self, device: Device, details: dict) -> None:
        with suppress(MyQError, OSError, TimeoutError):
            async with asyncio.timeout(10):
                await self._send(device, details, "trigger-siren-action", SIREN_OFF)

    async def _cancel_timer(self, key: str) -> bool:
        entry = self._sirens.pop(key, None)
        if entry is None:
            return False
        task, owned, _, _ = entry
        owned["stop"] = False  # A task cancelled before it starts never runs cleanup.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def stop_siren(self, device: Device, details: dict) -> None:
        if (entry := self._sirens.get(device.key)) is not None:
            # The details the siren was started with: the current ones may be missing
            # after a failed camera read, and the stop must still be addressed.
            details = entry[3]
        await self._send(device, details, "trigger-siren-action", SIREN_OFF)
        # Disarmed only once the stop has gone out; otherwise the timer still sends it.
        await self._cancel_timer(device.key)

    async def go_to_preset(self, device: Device, details: dict, preset_id: int) -> None:
        require_control(details, "ptz", permitted=True)
        if preset_id not in {preset for preset, _ in presets(details)}:
            raise CommandRejected("That saved camera position is no longer available")
        await self._send(device, details, "set-ptz", (("G", str(preset_id)),))

    async def close(self) -> None:
        """Stop every timed siren this integration started before the session closes."""
        for key, (_, _, device, details) in list(self._sirens.items()):
            await self._cancel_timer(key)
            await self._stop_quietly(device, details)

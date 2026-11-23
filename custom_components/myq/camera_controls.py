"""Explicit live-view buttons; transport submission never invents camera state."""

import asyncio

from homeassistant.components.button import ButtonEntity

from .api.camera_commands import presets
from .api.errors import CommandRejected
from .api.live_controls import require_control
from .api.models import section
from .entity import MyQEntity

_BUTTONS = (
    ("left", "mdi:arrow-left", "ptz"),
    ("right", "mdi:arrow-right", "ptz"),
    ("up", "mdi:arrow-up", "ptz"),
    ("down", "mdi:arrow-down", "ptz"),
    ("zoom_in", "mdi:magnify-plus", "ptz"),
    ("zoom_out", "mdi:magnify-minus", "ptz"),
    ("stop", "mdi:stop", "ptz"),
    ("siren_on", "mdi:alarm-light", "siren"),
    ("siren_off", "mdi:alarm-light-off", "siren"),
    ("talkback_on", "mdi:microphone", "talkback"),
    ("talkback_off", "mdi:microphone-off", "talkback"),
)
_STOP_ACTIONS = {"stop", "siren_off", "talkback_off"}
# The app sounds the siren and moves to saved positions without opening a view.
_WITHOUT_VIEW = {"siren_on", "siren_off"}


def camera_control_buttons(coordinator, device):
    if not device.is_camera:
        return []
    details = coordinator.data.cameras.get(device.key, {})
    supported = set()
    if isinstance(section(details, "cap", "cam").get("ptz"), dict):
        supported.add("ptz")
    if section(details, "cap", "siren").get("activatable") is True:
        supported.add("siren")
    if section(details, "cap", "spk").get("talkback") is True:
        supported.add("talkback")
    buttons = [
        MyQCameraControl(coordinator, device, action, icon, feature)
        for action, icon, feature in _BUTTONS
        if feature in supported
    ]
    if "ptz" in supported:
        buttons.extend(
            MyQCameraPreset(coordinator, device, preset_id, name)
            for preset_id, name in presets(details)
        )
    return buttons


class MyQCameraControl(MyQEntity, ButtonEntity):
    def __init__(self, coordinator, device, action, icon, feature) -> None:
        super().__init__(coordinator, device, "camera_control_" + action)
        self.action, self.feature = action, feature
        self._attr_translation_key, self._attr_icon = action, icon

    @property
    def available(self) -> bool:
        device = self.device
        if not super().available or device is None:
            return False
        if self._stream is None and self.action not in _WITHOUT_VIEW:
            return False
        if self.action in _STOP_ACTIONS:
            return True
        access = self.coordinator.data.account_access.get(device.account_id)
        try:
            require_control(
                self.camera_details,
                self.feature,
                permitted=access is not None and access.can_control_devices,
            )
        except CommandRejected:
            return False
        return True

    @property
    def _stream(self):
        return getattr(self.coordinator, "live_sessions", {}).get(self.device_key)

    async def async_press(self) -> None:
        if self._stream is None and self.action in _WITHOUT_VIEW:
            await self._press_without_view()
            return

        async def operation() -> None:
            stream = self._stream
            device = self.device
            if stream is None or device is None:
                raise CommandRejected("Open a live camera view before using these controls")
            # STOP must remain available when a fresh permission read fails or is revoked.
            if self.action in _STOP_ACTIONS:
                await self._dispatch(stream)
                return
            async with self.coordinator.video.settings_operation(device):
                # Both reads are fresh for every press; run them together to halve the lag.
                grants, details = await asyncio.gather(
                    self.coordinator.management.account_access(),
                    self.coordinator.video.details(device),
                )
                access = grants.get(device.account_id)
                stream.configure_controls(
                    details, can_control=access is not None and access.can_control_devices
                )
                if self._stream is not stream:
                    raise CommandRejected("The live camera session ended")
                await self._dispatch(stream)

        await self.coordinator.query(operation())

    async def _dispatch(self, stream) -> None:
        if self.feature == "ptz":
            await stream.ptz(self.action)
        elif self.feature == "siren":
            await stream.siren(self.action == "siren_on", duration=10)
        else:
            stream.set_talkback(self.action == "talkback_on")

    async def _press_without_view(self) -> None:
        commands = self.coordinator.camera_commands

        async def operation() -> None:
            device = self.device
            if device is None:
                raise CommandRejected("Camera is no longer registered")
            details = await _fresh_details(
                self.coordinator, device, stop=self.action == "siren_off"
            )
            if self.action == "siren_off":
                await commands.stop_siren(device, details)
            else:
                await commands.siren(device, details, seconds=10)

        await self.coordinator.query(operation())


async def _fresh_details(coordinator, device, *, stop: bool = False) -> dict:
    """Fresh grants and camera details for every press; a stop needs neither."""
    if stop:
        return coordinator.data.cameras.get(device.key, {})
    grants, details = await asyncio.gather(
        coordinator.management.account_access(), coordinator.video.details(device)
    )
    access = grants.get(device.account_id)
    if access is None or not access.can_control_devices:
        raise CommandRejected("Camera control is not permitted")
    return details


class MyQCameraPreset(MyQEntity, ButtonEntity):
    """Move a PTZ camera to one of its saved positions (set-ptz G)."""

    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, coordinator, device, preset_id: int, name: str) -> None:
        super().__init__(coordinator, device, f"camera_preset_{preset_id}")
        self.preset_id = preset_id
        self._attr_translation_key = "go_to_position"
        self._attr_translation_placeholders = {"position": name}

    @property
    def available(self) -> bool:
        device = self.device
        if not super().available or device is None:
            return False
        if self.preset_id not in {preset for preset, _ in presets(self.camera_details)}:
            return False
        access = self.coordinator.data.account_access.get(device.account_id)
        try:
            require_control(
                self.camera_details,
                "ptz",
                permitted=access is not None and access.can_control_devices,
            )
        except CommandRejected:
            return False
        return True

    async def async_press(self) -> None:
        async def operation() -> None:
            device = self.device
            if device is None:
                raise CommandRejected("Camera is no longer registered")
            details = await _fresh_details(self.coordinator, device)
            await self.coordinator.camera_commands.go_to_preset(device, details, self.preset_id)

        await self.coordinator.query(operation())

"""Refreshed camera snapshots and capability-gated Tend/KVS WebRTC live view."""

import asyncio
import time
from typing import cast

from aiortc import RTCIceServer
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.components.web_rtc import async_get_ice_servers
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from webrtc_models import RTCIceCandidateInit

from .api.errors import AuthenticationError, CommandRejected, MyQError
from .api.live import MAX_VIEWERS, LiveSession
from .api.video import camera_enabled, camera_online, wakeable
from .const import CONF_ENABLE_CAMERAS
from .entity import MyQEntity, discover_entities
from .exceptions import myq_error

PARALLEL_UPDATES = 0
LIVE_IMAGE_SECONDS = 2  # Reuse an encoded live frame briefly; frames keep arriving.


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    if entry.options.get(CONF_ENABLE_CAMERAS, True):
        discover_entities(
            entry.runtime_data,
            async_add_entities,
            lambda device: [MyQCamera(entry.runtime_data, device)] if device.is_camera else [],
        )


class MyQCamera(MyQEntity, Camera):
    _attr_translation_key = "camera"

    def __init__(self, coordinator, device) -> None:
        Camera.__init__(self)
        MyQEntity.__init__(self, coordinator, device, "recent_image")
        self._image: bytes | None = None
        self._image_time = 0.0
        self._image_lock = asyncio.Lock()
        self._image_generation = 0
        # Viewer session ID -> the camera's shared native session.
        self._live_sessions: dict[str, LiveSession] = {}
        self._senders: dict[str, WebRTCSendMessage] = {}
        self._image_source: str | None = None
        self._live_starts: dict[str, asyncio.Task] = {}
        self._live_cleanup: set[asyncio.Task] = set()

    @property
    def details(self):
        return self.camera_details

    @property
    def available(self) -> bool:
        # The app gates images on the MyQ online flag. A camera that sleeps may report
        # Tend offline while asleep; DevicesManager still wakes it for live view.
        return (
            super().available
            and bool(self.details)
            and (camera_online(self.details) or wakeable(self.details))
        )

    @property
    def _can_control(self) -> bool:
        access = (
            self.coordinator.data.account_access.get(self.device.account_id)
            if self.device is not None
            else None
        )
        return access is not None and access.can_control_devices

    @callback
    def _status_changed(self) -> None:
        """Treat targeted native messages as invalidation, never trusted entity state."""
        self._clear_image()
        if self._live_sessions:
            self.coordinator.request_camera_refresh(self.device_key)

    @callback
    def _unregister_stream(self, stream: LiveSession) -> None:
        if self.coordinator.live_sessions.get(self.device_key) is stream:
            self.coordinator.live_sessions.pop(self.device_key, None)
            self.coordinator.async_update_listeners()

    @property
    def supported_features(self):
        features = CameraEntityFeature.STREAM if self._can_stream else CameraEntityFeature(0)
        if (
            self.details.get("cap", {}).get("cam", {}).get("enableSwitch") is True
            and type(self.details.get("settings", {}).get("cam", {}).get("enabled")) is bool
        ):
            features |= CameraEntityFeature.ON_OFF
        return features

    @property
    def _can_stream(self) -> bool:
        alias = self.details.get("alias")
        return (
            self.details.get("cap", {}).get("cam", {}).get("kvsWebRTC") is True
            and isinstance(alias, str)
            and 0 < len(alias) <= 256
        )

    @property
    def is_on(self) -> bool:
        return camera_enabled(self.details)

    @property
    def _stream(self) -> LiveSession | None:
        """The shared native session for this camera, if one is open."""
        return next(iter(self._live_sessions.values()), None)

    @property
    def extra_state_attributes(self):
        if self._image_source == "live":
            return {"image_source": "live"}
        taken = self.coordinator.video.captured.get(self.device_key)
        attributes = {"image_source": "cloud"} if self._image is not None else {}
        if taken is not None:
            # TendSnapshotInterceptor: the image's capture time from tend-dttm.
            attributes["image_captured_at"] = taken.isoformat()
        return attributes or None

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        async with self._image_lock:
            if not self.available or not self.is_on:
                self._clear_image()
                return None
            stream = self._stream
            if stream is not None and getattr(stream, "ready", False):
                # The live player's snapshot: the newest decoded frame of the open view.
                if (
                    self._image is not None
                    and self._image_source == "live"
                    and time.monotonic() - self._image_time < LIVE_IMAGE_SECONDS
                ):
                    return self._image
                frame = await stream.snapshot()
                if frame is not None and self.available and self.is_on:
                    self._image, self._image_source = frame, "live"
                    self._image_time = time.monotonic()
                    self.async_write_ha_state()
                    return frame
            # CameraSnapshotProvider reuses a local copy for 60 seconds.
            if (
                self._image is not None
                and self._image_source == "cloud"
                and time.monotonic() - self._image_time < 60
            ):
                return self._image
            if (device := self.device) is None:
                return None
            generation = self._image_generation
            try:
                image = await self.coordinator.video.snapshot(device, self.details)
                if generation != self._image_generation or not self.available or not self.is_on:
                    return None
                self._image, self._image_source = image, "cloud"
                self._image_time = time.monotonic()
                self.async_write_ha_state()
                return self._image
            except AuthenticationError as err:
                self._clear_image()
                self.coordinator.config_entry.async_start_reauth(self.hass)
                raise myq_error(err) from err
            except MyQError as err:
                self._clear_image()
                raise myq_error(err) from err

    @callback
    def _clear_image(self) -> None:
        self._image = None
        self._image_source = None
        self._image_time = 0.0
        self._image_generation += 1

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.available or not self.is_on:
            self._clear_image()
        if not self.available or not self.is_on or not self._can_stream:
            for session_id in list(self._live_sessions):
                self.close_webrtc_session(session_id)
        else:
            for stream in {id(item): item for item in self._live_sessions.values()}.values():
                stream.configure_controls(self.details, can_control=self._can_control)
        super()._handle_coordinator_update()

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        # Captured once: a poll may drop the device while this offer is still starting.
        device = self.device
        if device is None or not self.available or not self.is_on or not self._can_stream:
            send_message(WebRTCError("unavailable", "This camera does not offer KVS live video"))
            return
        existing = self._stream
        if existing is not None or self._live_starts or self._live_cleanup:
            if (
                existing is not None
                and getattr(existing, "ready", False)
                and not self._live_starts
                and not self._live_cleanup
                and getattr(existing, "viewers", MAX_VIEWERS) < MAX_VIEWERS
            ):
                await self._join_view(existing, offer_sdp, session_id, send_message)
                return
            send_message(WebRTCError("busy", "A live view is already starting for this camera"))
            return

        async def failed(error: MyQError) -> None:
            await self.coordinator.async_release_live(stream)
            viewers = [key for key, item in self._live_sessions.items() if item is stream]
            for key in viewers:
                # LiveSession invokes this only after native/media cleanup.
                self._live_sessions.pop(key, None)
                if (sender := self._senders.pop(key, None)) is not None:
                    sender(WebRTCError("stream_failed", str(error)))
            if viewers:
                self._unregister_stream(stream)

        stream = LiveSession(
            async_get_clientsession(self.hass),
            failed,
            browser_ice_servers=[
                RTCIceServer(ice.urls, username=ice.username, credential=ice.credential)
                for ice in async_get_ice_servers(self.hass)
            ],
            allow_talkback=(
                self._can_control
                and self.details.get("cap", {}).get("spk", {}).get("talkback") is True
            ),
            on_status=self._status_changed,
        )
        self._live_sessions[session_id] = stream
        self._senders[session_id] = send_message
        self._live_starts[session_id] = cast(asyncio.Task, asyncio.current_task())
        try:
            await self.coordinator.async_acquire_live(stream)
            details = await self.coordinator.video.wakeup(device, self.details)
            if (
                details.get("cap", {}).get("cam", {}).get("kvsWebRTC") is not True
                or not camera_enabled(details)
                or not isinstance(details.get("alias"), str)
                or not details["alias"]
            ):
                raise CommandRejected("The camera is no longer available for KVS live video")
            can_control = False
            if self._can_control:
                access = (await self.coordinator.management.account_access()).get(device.account_id)
                can_control = access is not None and access.can_control_devices
            stream.configure_controls(details, can_control=can_control)
            token = await self.coordinator.client.auth.access_token()
            answer = await stream.start(details["alias"], token, offer_sdp, viewer=session_id)
            if self._live_sessions.get(session_id) is stream:
                self.coordinator.live_sessions[self.device_key] = stream
                self.coordinator.async_update_listeners()
                send_message(WebRTCAnswer(answer))
        except AuthenticationError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            send_message(WebRTCError("authentication", str(err)))
            await stream.close()
            await self.coordinator.async_release_live(stream)
            self._forget(session_id, stream)
        except MyQError as err:
            send_message(WebRTCError("stream_failed", str(err)))
            await stream.close()
            await self.coordinator.async_release_live(stream)
            self._forget(session_id, stream)
        except asyncio.CancelledError:
            await stream.close()
            await self.coordinator.async_release_live(stream)
            self._forget(session_id, stream)
            raise
        finally:
            if self._live_starts.get(session_id) is asyncio.current_task():
                self._live_starts.pop(session_id, None)

    async def _join_view(
        self,
        stream: LiveSession,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Add another viewer to the open native session (one MyQ session per camera)."""
        self._live_sessions[session_id] = stream
        self._senders[session_id] = send_message
        try:
            answer = await stream.join(offer_sdp, session_id)
            if self._live_sessions.get(session_id) is stream:
                send_message(WebRTCAnswer(answer))
        except MyQError as err:
            if self._live_sessions.get(session_id) is stream:
                send_message(WebRTCError("stream_failed", str(err)))
            self._forget(session_id, stream)
        except asyncio.CancelledError:
            self._forget(session_id, stream)
            await stream.leave(session_id)
            raise

    @callback
    def _forget(self, session_id: str, stream: LiveSession) -> None:
        if self._live_sessions.get(session_id) is stream:
            self._live_sessions.pop(session_id, None)
        self._senders.pop(session_id, None)

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        if (stream := self._live_sessions.get(session_id)) is not None:
            try:
                await stream.candidate(candidate.to_dict(), session_id)
            except MyQError as err:
                raise myq_error(err) from err

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        if (pending := self._live_starts.get(session_id)) is not None:
            if pending is not asyncio.current_task():
                pending.cancel()
        self._senders.pop(session_id, None)
        if (stream := self._live_sessions.pop(session_id, None)) is not None:
            if any(item is stream for item in self._live_sessions.values()):
                # Other viewers keep the native session; only this viewer's peer closes.
                task = self.hass.async_create_task(stream.leave(session_id), "leave MyQ live view")
            else:
                self._unregister_stream(stream)

                async def close() -> None:
                    try:
                        await stream.close()
                    finally:
                        await self.coordinator.async_release_live(stream)

                task = self.hass.async_create_task(close(), "close MyQ live view")
            self._live_cleanup.add(task)
            task.add_done_callback(self._live_cleanup.discard)
        super().close_webrtc_session(session_id)

    async def _close_live_views(self) -> None:
        pending = list(self._live_starts.values())
        for session_id in self._live_sessions.keys() | self._live_starts.keys():
            self.close_webrtc_session(session_id)
        tasks = [
            task for task in [*pending, *self._live_cleanup] if task is not asyncio.current_task()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def async_will_remove_from_hass(self) -> None:
        self._clear_image()
        await self._close_live_views()
        await super().async_will_remove_from_hass()

    async def async_turn_on(self) -> None:
        await self.coordinator.execute(
            self.coordinator.video.setting(
                self.device,
                "cam",
                "enabled",
                True,
            )
        )

    async def async_turn_off(self) -> None:
        self._clear_image()
        await self._close_live_views()
        await self.coordinator.execute(
            self.coordinator.video.setting(
                self.device,
                "cam",
                "enabled",
                False,
            )
        )

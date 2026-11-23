"""Camera snapshots and live video.

Cameras with MyQ's WebRTC capability are relayed to Home Assistant's WebRTC viewers.
TC cameras are a Home Assistant stream source instead: Home Assistant's own players
read the camera's H.264 from a local address, with nothing re-encoded.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import cast

from aiortc import RTCIceServer
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.components.camera.prefs import DynamicStreamSettings
from homeassistant.components.web_rtc import async_get_ice_servers
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from webrtc_models import RTCIceCandidateInit

from .api.associations import camera_opener
from .api.errors import AuthenticationError, CommandRejected, MyQError
from .api.live import MAX_VIEWERS, LiveSession
from .api.models import Device
from .api.tend_legacy import LegacyTendSession
from .api.tend_source import RETRY_SECONDS, TendSource
from .api.video import camera_enabled, camera_online, wakeable
from .const import CONF_ENABLE_CAMERAS
from .entity import MyQEntity, discover_entities
from .exceptions import myq_error

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
LIVE_IMAGE_SECONDS = 2  # Reuse an encoded live frame briefly; frames keep arriving.


def _tend_camera(device: Device | None) -> bool:
    return device is not None and device.is_camera and device.serial.startswith("TC")


def _live_route(device: Device | None, details: dict) -> str | None:
    """How a camera's live video is reached: "legacy" (TC stream source), "kvs" or not at all.

    A TC camera uses Tend's older video route whatever it reports about WebRTC; that
    route is the one known to work for these cameras. Other cameras need MyQ's WebRTC
    capability.
    """
    alias = details.get("alias")
    if not isinstance(alias, str) or not 0 < len(alias) <= 256:
        return None
    if _tend_camera(device):
        return "legacy"
    if details.get("cap", {}).get("cam", {}).get("kvsWebRTC") is True:
        return "kvs"
    return None


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    if entry.options.get(CONF_ENABLE_CAMERAS, True):
        coordinator = entry.runtime_data

        def cameras(device: Device) -> list[MyQEntity]:
            if not device.is_camera:
                return []
            # The class decides how Home Assistant plays video: its own stream players
            # for a TC camera, this integration's WebRTC relay otherwise.
            kind = MyQTendCamera if _tend_camera(device) else MyQCamera
            return [kind(coordinator, device)]

        discover_entities(coordinator, async_add_entities, cameras)


class MyQCameraBase(MyQEntity, Camera):
    """Snapshots, on/off and availability; subclasses supply the live video."""

    _attr_translation_key = "camera"

    def __init__(self, coordinator, device) -> None:
        Camera.__init__(self)
        MyQEntity.__init__(self, coordinator, device, "recent_image")
        self._image: bytes | None = None
        self._image_time = 0.0
        self._image_lock = asyncio.Lock()
        self._image_generation = 0
        self._image_source: str | None = None

    @property
    def details(self):
        return self.camera_details

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Stopping Home Assistant does not remove entities. Without this the camera
        # is never told to stop sending and its session is never signed out.
        self.async_on_remove(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._home_assistant_stopping)
        )

    async def _home_assistant_stopping(self, _event: Event) -> None:
        await self._close_live_views()

    @property
    def _can_stream(self) -> bool:
        return False

    @property
    def _live_open(self) -> bool:
        return False

    async def _live_image(self) -> bytes | None:
        """A frame of the open live view, where the view can supply one."""
        return None

    @callback
    def _update_live(self, allowed: bool) -> None:
        """Called on every refresh; end live video once it is no longer allowed."""

    async def _close_live_views(self) -> None:
        """End live video and wait for its cleanup."""

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
        if self._live_open:
            self.coordinator.request_camera_refresh(self.device_key)

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
    def is_on(self) -> bool:
        return camera_enabled(self.details)

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
            if self._live_open:
                # The live player's snapshot: the newest decoded frame of the open view.
                if (
                    self._image is not None
                    and self._image_source == "live"
                    and time.monotonic() - self._image_time < LIVE_IMAGE_SECONDS
                ):
                    return self._image
                frame = await self._live_image()
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
        # This also runs when a poll fails. A cloud read that failed says nothing
        # about the camera, so it must not end video that is playing.
        if (
            self.coordinator.last_update_success
            and self.device_key not in self.coordinator.data.camera_errors
        ):
            self._update_live(self.available and self.is_on and self._can_stream)
        super()._handle_coordinator_update()

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


class MyQCamera(MyQCameraBase):
    """A camera with MyQ's WebRTC capability, relayed to Home Assistant's viewers."""

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        # Viewer session ID -> the camera's shared native session.
        self._live_sessions: dict[str, LiveSession] = {}
        self._senders: dict[str, WebRTCSendMessage] = {}
        self._live_starts: dict[str, asyncio.Task] = {}
        self._live_cleanup: set[asyncio.Task] = set()

    @callback
    def _unregister_stream(self, stream: LiveSession) -> None:
        if self.coordinator.live_sessions.get(self.device_key) is stream:
            self.coordinator.live_sessions.pop(self.device_key, None)
            self.coordinator.async_update_listeners()

    @property
    def _can_stream(self) -> bool:
        return _live_route(self.device, self.details) == "kvs"

    @property
    def _stream(self) -> LiveSession | None:
        """The shared native session for this camera, if one is open."""
        return next(iter(self._live_sessions.values()), None)

    @property
    def _live_open(self) -> bool:
        return bool(self._live_sessions)

    async def _live_image(self) -> bytes | None:
        stream = self._stream
        if stream is None or not getattr(stream, "ready", False):
            return None
        return await stream.snapshot()

    @callback
    def _update_live(self, allowed: bool) -> None:
        if not allowed:
            for session_id in list(self._live_sessions):
                self.close_webrtc_session(session_id)
            return
        for stream in {id(item): item for item in self._live_sessions.values()}.values():
            stream.configure_controls(self.details, can_control=self._can_control)

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        # Captured once: a poll may drop the device while this offer is still starting.
        device = self.device
        if device is None or not self.available or not self.is_on or not self._can_stream:
            send_message(
                WebRTCError("unavailable", "This camera does not offer supported live video")
            )
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
            if _live_route(device, details) != "kvs" or not camera_enabled(details):
                raise CommandRejected("The camera is no longer available for live video")
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
        except Exception:
            # A defect must not leave this camera "busy" and the status feed paused.
            send_message(WebRTCError("stream_failed", "Live video could not start"))
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


class MyQTendCamera(MyQCameraBase):
    """A TC camera: Home Assistant's stream players read its H.264 from a local address.

    The class does not implement WebRTC itself, so Home Assistant uses its own WebRTC
    provider and stream worker with ``stream_source``. Snapshots come from MyQ.
    """

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        # The source is bare H.264, which carries no timestamps of its own.
        self.stream_options = {"use_wallclock_as_timestamps": True}
        self._source = TendSource(self._open_video, self._video_closed)
        self._stopping: set[asyncio.Task] = set()
        self._reported: str | None = None

    @property
    def _can_stream(self) -> bool:
        return _live_route(self.device, self.details) == "legacy"

    @property
    def _live_open(self) -> bool:
        return self._source.session is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.video_sources[self.device_key] = self._source

    async def stream_source(self) -> str | None:
        # Given whenever the camera can stream, so Home Assistant keeps its players
        # registered; whether video can start is decided when one of them connects.
        if not self._can_stream:
            return None
        try:
            return await self._source.url()
        except MyQError:
            return None  # The address is closed: this entity is being removed.

    def _opener_serials(self, device: Device) -> set[str]:
        """Serials Tend may list this camera under (the opener it is built into)."""
        opener = camera_opener(device, self.coordinator.data.devices)
        serials = {
            device.state.get("paired_gdo_serial_number"),
            device.state.get("parent_serial_number"),
            device.parent_device_id,
            opener.serial if opener is not None else None,
        }
        return {serial for serial in serials if isinstance(serial, str) and serial}

    async def _open_video(self, on_video: Callable[[bytes], None]) -> LegacyTendSession:
        """Open the camera session for the stream source's first reader."""
        device = self.device
        session: LegacyTendSession | None = None
        try:
            if device is None or not self.available or not self.is_on or not self._can_stream:
                raise CommandRejected("This camera is unavailable for live video")
            session = LegacyTendSession(self._opener_serials(device))
            session.on_video, session.on_status = on_video, self._status_changed
            await self.coordinator.async_acquire_live(session)
            details = await self.coordinator.video.wakeup(device, self.details)
            if _live_route(device, details) != "legacy" or not camera_enabled(details):
                raise CommandRejected("The camera is no longer available for live video")
            token = await self.coordinator.client.auth.access_token()
            await session.start(details["alias"], token)
        except BaseException as err:
            if session is not None:
                # Its own task: a start cancelled again while it cleans up (the reader
                # left and video was stopped) must still close and release the session.
                await asyncio.shield(
                    self.hass.async_create_task(self._abandon(session), "abandon MyQ camera video")
                )
            if isinstance(err, AuthenticationError):
                self.coordinator.config_entry.async_start_reauth(self.hass)
            if isinstance(err, MyQError):
                # Players retry on their own; report each distinct failure once.
                level = logging.DEBUG if str(err) == self._reported else logging.WARNING
                _LOGGER.log(level, "MyQ camera live video could not start: %s", err)
                self._reported = str(err)
            raise
        self._reported = None
        return session

    async def _abandon(self, session: LegacyTendSession) -> None:
        try:
            await session.close()
        finally:
            await self.coordinator.async_release_live(session)

    async def _video_closed(self, session: LegacyTendSession) -> None:
        await self.coordinator.async_release_live(session)

    @callback
    def _update_live(self, allowed: bool) -> None:
        if not allowed and (self._source.session is not None or self._source.readers):
            task = self.hass.async_create_task(self._source.stop(), "stop MyQ camera video")
            self._stopping.add(task)
            task.add_done_callback(self._stopping.discard)

    async def _close_live_views(self) -> None:
        # Players reconnect at once: hold them off while the camera is turned off.
        await self._source.stop(pause=RETRY_SECONDS)
        if self._stopping:
            await asyncio.gather(*self._stopping, return_exceptions=True)

    async def async_will_remove_from_hass(self) -> None:
        if self.coordinator.video_sources.get(self.device_key) is self._source:
            self.coordinator.video_sources.pop(self.device_key, None)
        try:
            await self._source.close()
        finally:
            if self.stream is not None:
                # stop() leaves a preloaded worker running; this camera is going away.
                self.stream.dynamic_stream_settings = DynamicStreamSettings()
                await self.stream.stop()
                self.stream = None
            await super().async_will_remove_from_hass()

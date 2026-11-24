"""A camera's speaker: talk through a TC camera, or play the sounds stored on a camera.

Text-to-speech, local media and any audio address can be played: the integration
downloads the audio, encodes it for the camera and sends it over the camera's talkback
connection. Playing something new stops what is playing.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.associations import camera_opener
from .api.errors import AuthenticationError, CommandRejected, MyQError
from .api.models import Device
from .api.tend_audio import (
    Talkback,
    can_talk,
    encode_audio,
    pick_codec,
    play_track,
    stored_tracks,
    talkback_codec,
)
from .api.tend_legacy import LegacyTendSession
from .api.video import camera_online, wakeable
from .camera import _live_route
from .const import CONF_ENABLE_CAMERAS
from .entity import MyQEntity, discover_entities
from .exceptions import myq_error

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
TRACK = "myq_track:"
MAX_DOWNLOAD = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30


def _talks(device: Device, details: dict) -> bool:
    return _live_route(device, details) == "legacy" and can_talk(details)


def has_speaker(device: Device, details: dict) -> bool:
    """A camera gets a Speaker: TC talkback, or sounds stored on the camera."""
    return _talks(device, details) or bool(stored_tracks(details))


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    if not entry.options.get(CONF_ENABLE_CAMERAS, True):
        return
    coordinator = entry.runtime_data

    def speakers(device: Device):
        if not device.is_camera:
            return
        details = coordinator.data.cameras.get(device.key, {})
        if has_speaker(device, details):
            yield MyQCameraSpeaker(coordinator, device)

    discover_entities(coordinator, async_add_entities, speakers)


class MyQCameraSpeaker(MyQEntity, MediaPlayerEntity):
    """Talkback and stored sounds; idle unless a talk is being sent."""

    _attr_translation_key = "speaker"
    _attr_media_content_type = MediaType.MUSIC

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "speaker")
        self._talk: asyncio.Task | None = None

    @property
    def _can_control(self) -> bool:
        device = self.device
        access = self.coordinator.data.account_access.get(device.account_id) if device else None
        return access is not None and access.can_control_devices

    @property
    def available(self) -> bool:
        details = self.camera_details
        return (
            super().available
            and self._can_control
            and bool(details)
            and (camera_online(details) or wakeable(details))
        )

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = MediaPlayerEntityFeature.BROWSE_MEDIA
        device = self.device
        if device is not None and _talks(device, self.camera_details):
            features |= (
                MediaPlayerEntityFeature.PLAY_MEDIA
                | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
                | MediaPlayerEntityFeature.STOP
            )
        elif stored_tracks(self.camera_details):
            features |= MediaPlayerEntityFeature.PLAY_MEDIA
        return features

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.PLAYING if self._talk is not None else MediaPlayerState.IDLE

    async def async_browse_media(
        self, media_content_type: MediaType | str | None = None, media_content_id: str | None = None
    ) -> BrowseMedia:
        if media_content_id and media_source.is_media_source_id(media_content_id):
            return await media_source.async_browse_media(
                self.hass, media_content_id, content_filter=_audio_only
            )
        device = self.device
        tracks = [
            BrowseMedia(
                media_class=MediaClass.MUSIC,
                media_content_id=TRACK + track_id,
                media_content_type=MediaType.MUSIC,
                title=title,
                can_play=True,
                can_expand=False,
            )
            for track_id, title in stored_tracks(self.camera_details)
        ]
        children = list(tracks)
        if device is not None and _talks(device, self.camera_details):
            sources = await media_source.async_browse_media(
                self.hass, None, content_filter=_audio_only
            )
            if not tracks:
                return sources
            children.append(sources)
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="",
            media_content_type="",
            title=self.name if isinstance(self.name, str) else "Speaker",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        device = self.device
        if device is None or not self.available:
            raise myq_error(CommandRejected("The camera's speaker is unavailable"))
        # Something new replaces what is playing.
        await self.async_media_stop()
        try:
            if media_id.startswith(TRACK):
                track_id = media_id.removeprefix(TRACK)
                if track_id not in dict(stored_tracks(self.camera_details)):
                    raise CommandRejected("The camera does not list this sound")
                await play_track(self.coordinator.client, device, track_id)
                return
            if not _talks(device, self.camera_details):
                raise CommandRejected("This camera can only play its stored sounds")
            data = await self._download(media_id)
            # Encoded before any session opens, so a file that is not audio fails at
            # once: in the codec the details list, else Opus until the camera says.
            codec = talkback_codec(self.camera_details) or "opus"
            frames = await self.hass.async_add_executor_job(encode_audio, data, codec)
            await self._start_talk(device, data, frames, codec)
        except AuthenticationError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise myq_error(err) from err
        except MyQError as err:
            raise myq_error(err) from err

    async def _download(self, media_id: str) -> bytes:
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            media_id = item.url
        url = async_process_play_media_url(self.hass, media_id)
        if not url.startswith(("http://", "https://")):
            raise CommandRejected("Only web addresses and media sources can be played")
        data = bytearray()
        try:
            async with async_get_clientsession(self.hass).get(
                url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            ) as response:
                response.raise_for_status()
                async for chunk in response.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > MAX_DOWNLOAD:
                        raise CommandRejected("The audio is too large to play")
        except (aiohttp.ClientError, TimeoutError):
            raise CommandRejected("The audio could not be downloaded") from None
        return bytes(data)

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

    @asynccontextmanager
    async def _session(self, device: Device):
        """The camera's open live-video session, or a login of the talk's own."""
        source = self.coordinator.video_sources.get(self.device_key)
        shared = getattr(source, "session", None)
        if isinstance(shared, LegacyTendSession) and shared.connected:
            yield shared
            return
        session = LegacyTendSession(self._opener_serials(device))
        await self.coordinator.async_acquire_live(session)
        try:
            details = await self.coordinator.video.wake_best_effort(device, self.camera_details)
            token = await self.coordinator.client.auth.access_token()
            alias = details.get("alias")
            if not isinstance(alias, str):
                raise CommandRejected("The camera is unavailable for talkback")
            await session.connect(alias, token)
            yield session
        finally:
            try:
                await session.close()
            finally:
                await self.coordinator.async_release_live(session)

    async def _start_talk(
        self, device: Device, data: bytes, frames: list[bytes], encoded: str
    ) -> None:
        """Open the talk before returning, so a failure reaches the caller; send in a task."""
        opened = asyncio.get_running_loop().create_future()

        async def run() -> None:
            try:
                async with self._session(device) as session:
                    talk = Talkback(session)
                    try:
                        await talk.open()
                        # The details' codec list, failing that the camera's login row,
                        # failing that the codec of the camera's own sound, as the app.
                        codec = (
                            talkback_codec(self.camera_details)
                            or pick_codec(session.speaker_codecs)
                            or await talk.heard_codec()
                        )
                        if codec is None:
                            raise CommandRejected("The camera does not name a talkback codec")
                        audio = frames
                        if codec != encoded:
                            audio = await self.hass.async_add_executor_job(
                                encode_audio, data, codec
                            )
                        opened.set_result(None)
                        await talk.stream(audio, codec)
                    finally:
                        await talk.close()
            except BaseException as err:
                if not opened.done():
                    if isinstance(err, asyncio.CancelledError):
                        opened.cancel()
                    else:
                        opened.set_exception(err)
                elif isinstance(err, MyQError):
                    _LOGGER.warning("MyQ camera talkback ended: %s", err)
                elif not isinstance(err, asyncio.CancelledError):
                    raise
            finally:
                if self._talk is asyncio.current_task():
                    self._talk = None
                    self.async_write_ha_state()

        self._talk = self.hass.async_create_task(run(), "MyQ camera talkback")
        self.async_write_ha_state()
        try:
            await opened
        except asyncio.CancelledError:
            await self.async_media_stop()
            raise

    async def async_media_stop(self) -> None:
        """Stop a talk in progress; the camera is sent STOP and the connection closed."""
        task, self._talk = self._talk, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        await self.async_media_stop()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.available and self._talk is not None:
            self.hass.async_create_task(self.async_media_stop(), "stop MyQ camera talkback")
        super()._handle_coordinator_update()


def _audio_only(item: BrowseMedia) -> bool:
    return item.media_content_type.startswith("audio/")

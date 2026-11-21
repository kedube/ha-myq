"""Browse MyQ recordings and serve short-lived, authenticated HLS or MP4 sessions."""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, unquote

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceError,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api.errors import AuthenticationError, MyQError
from .api.models import Device
from .api.recordings import HLS_TYPE, MP4_TYPE, PLAYBACK_MAX, Playback, Recording
from .const import CONF_ENABLE_CAMERAS, DOMAIN

DATA_SOURCE = f"{DOMAIN}_recording_media_source"
PROXY_PATH = f"/api/{DOMAIN}/recordings"
THUMBNAIL_PATH = f"/api/{DOMAIN}/recording_thumbnails"
SEGMENT_TYPES = frozenset(
    {
        "video/mp2t",
        "video/mp4",
        "video/iso.segment",
        "audio/mp4",
        "audio/aac",
        "audio/mpeg",
        "application/mp4",
        "application/octet-stream",
        "binary/octet-stream",
        # Some origins label AES-128 keys as text.
        "text/plain",
    }
)
PROGRESSIVE_TYPES = frozenset(
    {"video/mp4", "application/mp4", "application/octet-stream", "binary/octet-stream"}
)
LABELS = {
    "delivery": "Delivery",
    "garage door opened": "Garage door opened",
    "garage door closed": "Garage door closed",
    "person": "Person",
    "vehicle": "Vehicle",
    "car": "Car",
    "package": "Package",
    "animal": "Animal",
    "pet": "Pet",
    "motion": "Motion",
    "auto secure": "Auto secure",
}


def _path(*parts: str) -> str:
    return "/".join(quote(part, safe="") for part in parts)


def _parts(identifier: str) -> list[str]:
    if len(identifier) > 160000:
        raise MediaSourceError("Invalid recording selection")
    return [unquote(part) for part in identifier.split("/")] if identifier else []


def recording_content_id(entry_id: str, device: Device, event_id: str) -> str:
    """The media-source URI that resolves one recording of this camera."""
    return f"media-source://{DOMAIN}/" + _path(
        entry_id, device.account_id, device.serial, "event", event_id
    )


def _folder(identifier: str, title: str, children=None) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        title=title,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        can_play=False,
        can_expand=True,
        children=children,
    )


def _thumbnail_ref(*parts: str) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


def _parse_thumbnail_ref(value: str) -> list[str]:
    try:
        if len(value) > 4096:
            raise ValueError
        parts = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_"))
        if (
            not isinstance(parts, list)
            or len(parts) != 4
            or not all(isinstance(part, str) and 0 < len(part) <= 512 for part in parts)
        ):
            raise ValueError
        return parts
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise web.HTTPNotFound() from None


def title(event: Recording) -> str:
    """Local time, duration and the app's detection labels (x71/u)."""
    text = dt_util.as_local(datetime.fromisoformat(event.timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    if event.duration:
        text += f" · {round(event.duration)} s"
    names = [LABELS[label] for label in event.labels if label in LABELS]
    if names:
        text += " · " + ", ".join(names)
    elif event.kind:
        text += f" · {event.kind}"
    if event.status_hint:
        text += f" ({event.status_hint})"
    return text


@dataclass(repr=False)
class PlaybackGrant:
    entry_id: str
    account_id: str
    serial: str
    coordinator: object
    playback: Playback


async def async_get_media_source(hass: HomeAssistant) -> MyQMediaSource:
    if DATA_SOURCE not in hass.data:
        source = hass.data[DATA_SOURCE] = MyQMediaSource(hass)
        hass.http.register_view(RecordingView(source))
        hass.http.register_view(RecordingThumbnailView(source))
    return hass.data[DATA_SOURCE]


class MyQMediaSource(MediaSource):
    name = "MyQ recordings"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass
        self.grants: dict[str, PlaybackGrant] = {}

    def _entry(self, entry_id: str):
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
            or not entry.options.get(CONF_ENABLE_CAMERAS, True)
        ):
            raise MediaSourceError("The MyQ camera integration is unavailable")
        return entry

    def _device(self, entry_id: str, account_id: str, serial: str):
        entry = self._entry(entry_id)
        coordinator = entry.runtime_data
        if not self._may_browse(coordinator, account_id):
            raise MediaSourceError("This account does not grant permission to view recordings")
        device = next(
            (
                device
                for device in coordinator.data.devices.values()
                if device.account_id == account_id and device.serial == serial and device.is_camera
            ),
            None,
        )
        if device is None:
            raise MediaSourceError("The selected camera is no longer available")
        return entry, device

    @staticmethod
    def _may_browse(coordinator, account_id: str) -> bool:
        access = getattr(coordinator.data, "account_access", {}).get(account_id)
        return bool(
            access and access.allows("devices", "View") and access.allows("history", "View")
        )

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = _parts(item.identifier)
        if not parts:
            entries = []
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                try:
                    self._entry(entry.entry_id)
                except MediaSourceError:
                    continue
                entries.append(_folder(_path(entry.entry_id), entry.title))
            return _folder("", self.name, entries)
        entry = self._entry(parts[0])
        devices = [
            device
            for device in entry.runtime_data.data.devices.values()
            if device.is_camera and self._may_browse(entry.runtime_data, device.account_id)
        ]
        if len(parts) == 1:
            accounts = sorted({device.account_id for device in devices})
            return _folder(
                item.identifier,
                entry.title,
                [
                    _folder(_path(entry.entry_id, account), f"Account {index + 1}")
                    for index, account in enumerate(accounts)
                ],
            )
        if len(parts) == 2:
            cameras = [device for device in devices if device.account_id == parts[1]]
            if not cameras:
                raise MediaSourceError("The selected account has no accessible cameras")
            return _folder(
                item.identifier,
                "Cameras",
                [_folder(_path(*parts, device.serial), device.name) for device in cameras],
            )
        door = len(parts) >= 4 and parts[3] == "doors"
        rest = parts[4:] if door else parts[3:]
        if len(rest) not in (0, 2) or (rest and rest[0] != "page"):
            raise MediaSourceError("Invalid recording folder")
        entry, device = self._device(*parts[:3])
        try:
            page = await entry.runtime_data.recordings.page(
                device, rest[1] if rest else None, door=door
            )
            self._device(*parts[:3])
        except MyQError as err:
            if isinstance(err, AuthenticationError):
                entry.async_start_reauth(self.hass)
            raise MediaSourceError(str(err)) from None
        children = []
        if not door and not rest:
            children.append(_folder(_path(*parts[:3], "doors"), "Door events"))
        children += [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=_path(*parts[:3], "event", event.id),
                title=title(event),
                media_class=MediaClass.VIDEO,
                media_content_type=HLS_TYPE,
                # The app plays without a status check (l71/f); status only labels.
                can_play=True,
                can_expand=False,
                thumbnail=f"{THUMBNAIL_PATH}/{_thumbnail_ref(*parts[:3], event.id)}",
            )
            for event in page.items
        ]
        if page.next_cursor:
            base = (*parts[:3], "doors") if door else tuple(parts[:3])
            children.append(_folder(_path(*base, "page", page.next_cursor), "Older recordings"))
        return _folder(item.identifier, "Door events" if door else device.name, children)

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = _parts(item.identifier)
        if len(parts) != 5 or parts[3] != "event":
            raise Unresolvable("Select a recording to play")
        entry, device = self._device(*parts[:3])
        try:
            playback = await entry.runtime_data.recordings.playback(device, parts[4])
            self._device(*parts[:3])
        except MyQError as err:
            if isinstance(err, AuthenticationError):
                entry.async_start_reauth(self.hass)
            raise Unresolvable(str(err)) from None
        self._prune()
        if len(self.grants) >= 32:
            self.grants.pop(next(iter(self.grants)))
        token = secrets.token_urlsafe(24)
        self.grants[token] = PlaybackGrant(
            parts[0], parts[1], parts[2], entry.runtime_data, playback
        )
        return PlayMedia(self._link(token, "0"), playback.content_type)

    def _link(self, token: str, resource: str) -> str:
        # The grant's sliding expiry governs access; the signature only bounds it.
        return async_sign_path(
            self.hass, f"{PROXY_PATH}/{token}/{resource}", timedelta(seconds=PLAYBACK_MAX)
        )

    def _prune(self) -> None:
        now = time.monotonic()
        self.grants = {
            key: grant for key, grant in self.grants.items() if grant.playback.expires_at > now
        }

    def grant(self, token: str) -> tuple[PlaybackGrant, Device]:
        self._prune()
        if (grant := self.grants.get(token)) is None:
            raise web.HTTPGone(text="Recording playback expired; select it again")
        try:
            entry, device = self._device(grant.entry_id, grant.account_id, grant.serial)
        except MediaSourceError:
            self.grants.pop(token, None)
            raise web.HTTPGone(text="Camera is no longer available") from None
        if entry.runtime_data is not grant.coordinator:
            self.grants.pop(token, None)
            raise web.HTTPGone(text="Recording playback expired")
        return grant, device


def _header(headers: dict[str, str], name: str) -> str | None:
    name = name.lower()
    return next((value for key, value in headers.items() if key.lower() == name), None)


class RecordingView(HomeAssistantView):
    """HA auth/signatures protect media; upstream bearer headers stay on MyQ/Tend hosts."""

    url = PROXY_PATH + "/{token}/{resource}"
    name = f"api:{DOMAIN}:recordings"
    requires_auth = True

    def __init__(self, source: MyQMediaSource) -> None:
        self.source = source

    async def get(self, request: web.Request, token: str, resource: str) -> web.StreamResponse:
        grant, _ = self.source.grant(token)
        if grant.playback.progressive:
            return await self._stream(request, token, grant, resource)
        try:
            response, url = await grant.playback.fetch(resource, request.headers.get("Range"))
            # Re-check membership after network I/O, including an unload during fetch.
            self.source.grant(token)
            declared = response.header("Content-Type")
            content_type = (
                "application/octet-stream" if declared is None else declared.split(";")[0].lower()
            )
            body = response.body
            if body.lstrip(b"\xef\xbb\xbf").startswith(b"#EXTM3U") or content_type in (
                HLS_TYPE,
                "application/x-mpegurl",
                "audio/mpegurl",
            ):
                body = grant.playback.rewrite_manifest(
                    body,
                    url,
                    lambda key: self.source._link(token, key),
                )
                content_type = HLS_TYPE
            elif content_type not in SEGMENT_TYPES:
                raise Unresolvable("Recording returned an unsupported media type")
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            if response.status == 206:
                content_range = response.header("Content-Range") or ""
                if not re.fullmatch(r"bytes [0-9]+-[0-9]+/(?:[0-9]+|\*)", content_range):
                    raise Unresolvable("Recording returned an invalid byte range")
                headers["Content-Range"] = content_range
                headers["Accept-Ranges"] = "bytes"
            return web.Response(
                body=body,
                status=response.status,
                content_type=content_type,
                headers=headers,
            )
        except (MyQError, Unresolvable):
            raise web.HTTPBadGateway(
                text="Recording media is unavailable; select it again"
            ) from None

    async def _stream(
        self, request: web.Request, token: str, grant: PlaybackGrant, resource: str
    ) -> web.StreamResponse:
        """Progressive MP4 (videoCdn) is relayed in chunks, honoring byte ranges."""
        try:
            stream = await grant.playback.open_stream(resource, request.headers.get("Range"))
        except MyQError:
            raise web.HTTPBadGateway(
                text="Recording media is unavailable; select it again"
            ) from None
        try:
            self.source.grant(token)
            declared = _header(stream.headers, "Content-Type")
            if (
                declared is not None
                and declared.split(";")[0].strip().lower() not in PROGRESSIVE_TYPES
            ):
                raise web.HTTPBadGateway(text="Recording returned an unsupported media type")
            headers = {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Accept-Ranges": "bytes",
            }
            if stream.status == 206:
                content_range = _header(stream.headers, "Content-Range") or ""
                if not re.fullmatch(r"bytes [0-9]+-[0-9]+/(?:[0-9]+|\*)", content_range):
                    raise web.HTTPBadGateway(text="Recording returned an invalid byte range")
                headers["Content-Range"] = content_range
            response = web.StreamResponse(status=stream.status, headers=headers)
            response.content_type = MP4_TYPE
            length = _header(stream.headers, "Content-Length")
            if length is not None and length.isdigit() and len(length) <= 15:
                response.content_length = int(length)
            await response.prepare(request)
            try:
                async for chunk in stream.chunks:
                    await response.write(chunk)
            except MyQError:
                # Headers are already sent; end the body early rather than corrupt it.
                return response
            await response.write_eof()
            return response
        finally:
            await stream.close()


class RecordingThumbnailView(HomeAssistantView):
    """Event thumbnails for the media browser; the frontend signs these paths."""

    url = THUMBNAIL_PATH + "/{ref}"
    name = f"api:{DOMAIN}:recording_thumbnails"
    requires_auth = True

    def __init__(self, source: MyQMediaSource) -> None:
        self.source = source

    async def get(self, request: web.Request, ref: str) -> web.Response:
        entry_id, account_id, serial, event_id = _parse_thumbnail_ref(ref)
        try:
            entry, device = self.source._device(entry_id, account_id, serial)
        except MediaSourceError:
            raise web.HTTPNotFound() from None
        try:
            body, content_type = await entry.runtime_data.recordings.thumbnail(device, event_id)
        except MyQError as err:
            if isinstance(err, AuthenticationError):
                entry.async_start_reauth(self.source.hass)
            raise web.HTTPBadGateway(text="Recording thumbnail is unavailable") from None
        return web.Response(
            body=body,
            content_type=content_type,
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

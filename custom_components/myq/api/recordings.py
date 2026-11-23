"""Tend recorded events, thumbnails and authenticated HLS or MP4, separate from live video.

Evidence: h61/i and y51/y (event query), j61/TendMotionEvent and j61/TendEventTags
(event fields), l71/f and a81/d (player: no status gate, HLS then videoCdn fallback),
m61/o (headers on every media request), hj0/m and bf1/a (thumbnails), m61/l and
tu/j (door-linked clips).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import aiohttp

from .client import Client
from .const import TIMEOUT, USER_AGENT
from .errors import (
    AccessDenied,
    AuthenticationError,
    CannotConnect,
    MyQError,
    ProtocolError,
    RateLimited,
)
from .http import Http, Response, check_status, renewable, retry_seconds
from .models import Device
from .video import VIDEO

EVENTS = "/cxs/api/events"
EVENT_MEDIA = "https://events-media.prod.video.myq.com"
MEDIA_ORIGINS = frozenset((VIDEO, EVENT_MEDIA))
PAGE_SIZE = 50
MAX_BOUNDARY = 500
MAX_LISTED = 4096  # Events remembered as belonging to the camera they were listed for.
PLAYBACK_TTL = 600  # Idle lifetime; each request extends it.
PLAYBACK_MAX = 7200  # Absolute lifetime of one playback grant.
INFO_TTL = 600
PROBE_TTL = 30
MAX_THUMBNAIL = 2 * 1024 * 1024
MAX_STREAM = 1024 * 1024 * 1024
HLS_TYPE = "application/vnd.apple.mpegurl"
MP4_TYPE = "video/mp4"
DOOR_TAG = "accessEventID*"
REDIRECTS = (301, 302, 303, 307, 308)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_TEND_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.tendplatform\.com")
# y51/r: everything short of FINALIZED, and the error states, explain a failed play.
STATUS_HINTS = {
    "OPEN": "is still recording",
    "PREFINALIZING": "is still processing",
    "PROCESSING": "is still processing",
    "FINALIZING": "is still processing",
    "ERROR_PROCESSING": "failed to process",
    "ERROR_FINALIZING": "failed to process",
    "DELETED": "was deleted",
}
# x71/u label order; tag names from j61/TendEventTags and the event filters (f61/l).
COUNT_LABELS = (
    ("peopleCount", "person"),
    ("movingVehicleCount", "vehicle"),
    ("carCount", "car"),
    ("movingPackageCount", "package"),
    ("animalCount", "animal"),
    ("dogCount", "pet"),
    ("catCount", "pet"),
    ("objectCount", "motion"),
)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or not value.strip("."):
        raise ProtocolError("Invalid recording identifier")
    return value


def _date(value: object) -> str:
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            raise ValueError
        return date.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except ValueError:
        raise ProtocolError("Invalid recording timestamp") from None


def _earlier(timestamp: str) -> str:
    """One millisecond before a normalized boundary, to step past undecodable rows."""
    date = datetime.fromisoformat(timestamp) - timedelta(milliseconds=1)
    return date.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _finite(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False  # An integer too large for a float.


def _count(value: object) -> float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value if _finite(value) else 0
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return 0
        return number if math.isfinite(number) else 0
    return 0


def _true(value: object) -> bool:
    # JsonParseUtil.bool accepts a Boolean or its string form.
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _tag_text(value: object) -> str | None:
    return value if isinstance(value, str) and value and len(value) <= 100 else None


def event_labels(tags: object) -> tuple[tuple[str, ...], str | None, str | None]:
    """Detection labels plus the door link (accessEventID.GD_OPEN/GD_CLOSE)."""
    if not isinstance(tags, dict):
        return (), None, None
    found: list[str] = []

    def add(label: str) -> None:
        if label not in found:
            found.append(label)

    if _true(tags.get("delivery")):
        add("delivery")
    door, door_time = None, None
    for key, direction in (("accessEventID.GD_OPEN", "open"), ("accessEventID.GD_CLOSE", "close")):
        if (value := _tag_text(tags.get(key))) is not None and door is None:
            door, door_time = direction, value
    for key in tags:
        # EventsMasterService.calcEventTagsGarageDoor: grg.door.<source>.open|close.
        if isinstance(key, str) and key.startswith("grg.door.") and len(key) <= 100:
            parts = key[len("grg.door.") :].split(".")
            if len(parts) == 2 and parts[1] in ("open", "close"):
                add("garage door opened" if parts[1] == "open" else "garage door closed")
    if door is not None:
        add("garage door opened" if door == "open" else "garage door closed")
    for key, label in COUNT_LABELS:
        if _count(tags.get(key)) > 0:
            add(label)
    if tags.get("autoSecureEventId"):
        add("auto secure")
    return tuple(found), door, door_time


@dataclass(frozen=True, repr=False)
class Recording:
    id: str
    camera_id: str
    timestamp: str
    kind: str
    status: str | None = None
    duration: float | None = None
    labels: tuple[str, ...] = ()
    door: str | None = None
    door_time: str | None = None

    @property
    def status_hint(self) -> str | None:
        return STATUS_HINTS.get(self.status or "")

    @classmethod
    def parse(cls, data: object, device: Device) -> Recording:
        """Strict: a mismatched camera or account is refused, never shown."""
        if not isinstance(data, dict):
            raise ProtocolError("Invalid recording data")
        if data.get("srcId") != device.camera_id or (
            "myqAccountId" in data and data["myqAccountId"] != device.account_id
        ):
            raise AccessDenied("Recording does not belong to the selected camera")
        kind = data.get("type")
        status = data.get("status")
        duration = data.get("duration")
        found, door, door_time = event_labels(data.get("tags"))
        return cls(
            _identifier(data.get("id")),
            device.camera_id,
            _date(data.get("dttm")),
            # TendMotionEvent.type is nullable; the app reads null as "".
            kind if isinstance(kind, str) and len(kind) <= 100 else "",
            status.upper() if isinstance(status, str) and len(status) <= 40 else None,
            _duration(duration),
            found,
            door,
            door_time,
        )


def _duration(value: object) -> float | None:
    """TendMotionEvent.duration: seconds as a Float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if _finite(value) and value >= 0 else None


def _row(data: object, device: Device) -> Recording | None:
    """EventsApiResponse drops rows it cannot place; one bad row never hides the rest."""
    try:
        return Recording.parse(data, device)
    except MyQError:
        return None


def _row_date(data: object) -> str | None:
    try:
        return _date(data.get("dttm")) if isinstance(data, dict) else None
    except ProtocolError:
        return None


@dataclass(frozen=True, repr=False)
class RecordingPage:
    items: tuple[Recording, ...]
    next_cursor: str | None


def _cursor(end: str, seen: list[str]) -> str:
    return base64.urlsafe_b64encode(json.dumps([end, seen]).encode()).decode().rstrip("=")


def _parse_cursor(value: str | None) -> tuple[str | None, set[str]]:
    if value is None:
        return None, set()
    try:
        if len(value) > 150000:
            raise ValueError
        end, seen = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(seen, list) or len(seen) > MAX_BOUNDARY:
            raise ValueError
        return _date(end), {_identifier(item) for item in seen}
    except (ValueError, TypeError, binascii.Error, RecursionError, ProtocolError):
        raise ProtocolError("Invalid recording page") from None


def _redirected(url: str, location: str) -> str:
    try:
        return urljoin(url, location)
    except ValueError:
        raise ProtocolError("Recording redirect destination is invalid") from None


def authenticated_host(host: str) -> bool:
    """m61/o sends Bearer and AntiCSRF with every media request; HA limits that to the
    MyQ event-media host and Tend hosts, never a CDN."""
    return f"https://{host}" in MEDIA_ORIGINS or bool(_TEND_HOST.fullmatch(host))


def validate_media_url(url: str, event_id: str) -> str:
    """Only event-scoped Tend routes and HTTPS CDN destinations are fetchable."""
    try:
        parsed = urlsplit(url)
        if (
            len(url) > 16384
            or parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(ord(c) < 33 for c in url)
            or "\\" in url
        ):
            raise ValueError
        host = parsed.hostname or ""
        origin = f"https://{host}"
        path = unquote(parsed.path)
        if (
            any(part in (".", "..") for part in path.split("/"))
            or "%" in path
            or "\\" in path
            or any(ord(char) < 32 for char in path)
        ):
            raise ValueError
        if origin in MEDIA_ORIGINS:
            if not path.startswith(f"{EVENTS}/{_identifier(event_id)}/media/"):
                raise ValueError
        elif not (
            re.fullmatch(r"[a-z0-9-]+\.cloudfront\.net", host)
            or re.fullmatch(r"[a-z0-9.-]+\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host)
            or _TEND_HOST.fullmatch(host)
        ):
            raise ValueError
        query = parse_qs(parsed.query, max_num_fields=100)
        if "X-Amz-Date" in query or "X-Amz-Expires" in query:
            issued = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ")
            expires = issued.replace(tzinfo=UTC).timestamp() + int(query["X-Amz-Expires"][0])
            if expires <= time.time():
                raise ValueError
        if "Expires" in query:
            expires = float(query["Expires"][0])
            if not math.isfinite(expires) or expires <= time.time():
                raise ValueError
        return url
    except (ValueError, KeyError, IndexError, OverflowError, ProtocolError):
        raise ProtocolError("Recording media address is invalid or expired") from None


async def _media_headers(client: Client, url: str, extra: dict[str, str]) -> dict[str, str]:
    headers = dict(extra)
    if authenticated_host(urlsplit(url).hostname or ""):
        headers["AntiCSRF"] = "AntiCSRF"
        headers["Authorization"] = "Bearer " + await client.auth.access_token()
    return headers


async def fetch_media(
    client: Client, url: str, event_id: str, extra: dict[str, str] | None = None
) -> tuple[Response, str]:
    """GET one media resource, re-validating each redirect hop; CDNs get no credentials."""
    for _ in range(4):
        validate_media_url(url, event_id)
        headers = await _media_headers(client, url, extra or {})
        response = await client.http.request("GET", url, headers=headers)
        if "Authorization" in headers and renewable(response):
            token = await client.auth.access_token(
                rejected_token=headers["Authorization"].removeprefix("Bearer ")
            )
            headers["Authorization"] = "Bearer " + token
            response = await client.http.request("GET", url, headers=headers)
        if response.status in REDIRECTS:
            location = response.header("Location")
            if not location:
                raise ProtocolError("Recording redirect did not contain a destination")
            url = _redirected(url, location)
            continue
        check_status(response)
        return response, url
    raise ProtocolError("Recording exceeded the redirect limit")


def _pattern(info: Any, name: str) -> str | None:
    resource = info.get(name) if isinstance(info, dict) else None
    pattern = resource.get("urlPattern") if isinstance(resource, dict) else None
    return pattern if isinstance(pattern, str) and pattern.count("{eventId}") == 1 else None


class _TooManyResources(ProtocolError):
    """A playlist names more media addresses than one playback may hold."""


class Recordings:
    """On-demand event discovery; does not poll, delete, or mark events viewed."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self._info: tuple[float, dict] | None = None
        # Event ID -> key of the camera MyQ listed it for, newest last; bounded.
        self._cameras: dict[str, str] = {}

    def _listed(self, device: Device, events: Iterable[Recording]) -> None:
        for event in events:
            self._cameras.pop(event.id, None)
            self._cameras[event.id] = device.key
        while len(self._cameras) > MAX_LISTED:
            del self._cameras[next(iter(self._cameras))]

    def _params(self, device: Device, **extra: str) -> dict[str, str]:
        return {
            "myqAccountId": device.account_id,
            "srcId": device.camera_id,
            "v": "2",
            "hideUnplayable": "true",
            # m61/l sends includeHidden=true for door-linked clips and their media.
            "includeHidden": "true",
            **extra,
        }

    async def _events(self, params: dict[str, str]) -> tuple[list, Any]:
        response = await self.client.request(
            "GET", VIDEO, EVENTS, headers={"AntiCSRF": "AntiCSRF"}, params=params
        )
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ProtocolError("Invalid recording list")
        return data["items"], data

    async def page(
        self, device: Device, cursor: str | None = None, *, door: bool = False
    ) -> RecordingPage:
        """One page, newest first. door=True lists door-linked clips (tag accessEventID*)."""
        if not device.is_camera:
            raise AccessDenied("Recordings require a camera")
        end, seen = _parse_cursor(cursor)
        params = self._params(device, limit=str(PAGE_SIZE + len(seen)))
        if door:
            params["tag"] = DOOR_TAG
        if end:
            params["endDttm"] = end
        rows, data = await self._events(params)
        events: dict[str, Recording] = {}
        dated = []
        for row in rows:
            if (when := _row_date(row)) is not None:
                dated.append(when)
            event = _row(row, device)
            if (
                event is None
                or event.id in events
                # Newer than the boundary: already shown on an earlier page.
                or (end is not None and event.timestamp > end)
                # FilteredEventsView: events without a positive duration are not listed.
                or (event.duration is not None and event.duration <= 0)
                or (door and not _door_clip(event))
            ):
                continue
            events[event.id] = event
        ordered = sorted(events.values(), key=lambda item: (item.timestamp, item.id), reverse=True)
        remaining = [event for event in ordered if event.id not in seen]
        selected = remaining[:PAGE_SIZE]
        more = data.get("moreAvailable")
        if type(more) is not bool:
            more = len(rows) >= PAGE_SIZE + len(seen)
        next_cursor = None
        if more or len(remaining) > PAGE_SIZE:
            if selected:
                boundary = selected[-1].timestamp
                excluded = {event.id for event in selected if event.timestamp == boundary}
                if boundary == end:
                    excluded |= seen
                if len(excluded) > MAX_BOUNDARY:
                    raise ProtocolError("Too many recordings share one page boundary")
                next_cursor = _cursor(boundary, sorted(excluded))
            elif dated and (oldest := min(dated)) and (end is None or oldest <= end):
                # Nothing on this page was displayable; continue strictly before it.
                next_cursor = _cursor(_earlier(oldest), [])
        self._listed(device, selected)
        return RecordingPage(tuple(selected), next_cursor)

    async def door_clips(self, device: Device, start: str, end: str) -> tuple[Recording, ...]:
        """Door-linked video clips in a window, as tu/j matches them to door history."""
        if not device.is_camera:
            raise AccessDenied("Recordings require a camera")
        params = self._params(
            device,
            tag=DOOR_TAG,
            startDttm=_date(start),
            endDttm=_date(end),
            limit=str(PAGE_SIZE),
        )
        rows, _ = await self._events(params)
        clips: dict[str, Recording] = {}
        for row in rows:
            event = _row(row, device)
            if event is not None and _door_clip(event) and event.id not in clips:
                clips[event.id] = event
        self._listed(device, clips.values())
        return tuple(sorted(clips.values(), key=lambda item: (item.timestamp, item.id)))

    async def _media_info(self) -> dict:
        cached = self._info
        if cached is not None and time.monotonic() - cached[0] < INFO_TTL:
            return cached[1]
        response = await self.client.request(
            "GET", VIDEO, EVENTS + "/mediaDownloadInfo", headers={"AntiCSRF": "AntiCSRF"}
        )
        info = response.json()
        if not isinstance(info, dict):
            raise ProtocolError("Recording media information is invalid")
        self._info = time.monotonic(), info
        return info

    async def event(self, device: Device, event_id: str) -> Recording:
        if not device.is_camera:
            raise AccessDenied("Recordings require a camera")
        event_id = _identifier(event_id)
        response = await self.client.request(
            "GET",
            VIDEO,
            f"{EVENTS}/{quote(event_id, safe='')}",
            headers={"AntiCSRF": "AntiCSRF"},
            params={"v": "2"},
        )
        event = Recording.parse(response.json(), device)
        if event.id != event_id:
            raise AccessDenied("Recording is not available for playback")
        self._listed(device, (event,))
        return event

    async def playback(self, device: Device, event_id: str) -> Playback:
        """HLS first, then the progressive videoCdn resource (a81/d k()).

        Like the app there is no status gate; the status only explains a failure.
        """
        event = await self.event(device, event_id)
        info = await self._media_info()
        failure: MyQError | None = None
        if (pattern := _pattern(info, "video.m3u8")) is not None:
            try:
                url = validate_media_url(pattern.replace("{eventId}", event.id), event.id)
                playback = Playback(self.client, event.id, url)
                await playback.probe()
                return playback
            except (AuthenticationError, RateLimited):
                raise
            except MyQError as err:
                failure = err
        if (pattern := _pattern(info, "videoCdn")) is not None:
            try:
                url = validate_media_url(pattern.replace("{eventId}", event.id), event.id)
                return Playback(self.client, event.id, url, content_type=MP4_TYPE)
            except ProtocolError as err:
                failure = err
        if event.status_hint is not None:
            raise ProtocolError(f"This recording {event.status_hint}")
        if failure is not None:
            raise failure
        raise ProtocolError("Recording media was not advertised")

    async def thumbnail(self, device: Device, event_id: str) -> tuple[bytes, str]:
        """mediaDownloadInfo["thumbnail"] (the event list's thumbnailUrl), or hj0/m.y."""
        if not device.is_camera:
            raise AccessDenied("Recordings require a camera")
        event_id = _identifier(event_id)
        if self._cameras.get(event_id) != device.key:
            # Not an event MyQ listed for this camera: confirm that it belongs to it,
            # as playback does, before serving its picture under this camera.
            await self.event(device, event_id)
        try:
            pattern = _pattern(await self._media_info(), "thumbnail")
        except (AuthenticationError, RateLimited):
            raise
        except MyQError:
            pattern = None
        if pattern is not None:
            url = pattern.replace("{eventId}", event_id)
        else:
            url = f"{VIDEO}{EVENTS}/{quote(event_id, safe='')}/media/thumbnail?includeHidden=true"
        response, _ = await fetch_media(self.client, url, event_id)
        body = response.body
        if len(body) > MAX_THUMBNAIL:
            raise ProtocolError("Recording thumbnail is too large")
        if body.startswith(b"\xff\xd8\xff"):
            return body, "image/jpeg"
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return body, "image/png"
        raise ProtocolError("Recording thumbnail is not an image")


def _door_clip(event: Recording) -> bool:
    # tu/j: a VIDEO event whose accessEventID.GD_OPEN/GD_CLOSE names the door event.
    return event.door is not None and event.kind.upper() == "VIDEO"


@dataclass(repr=False)
class MediaStream:
    status: int
    headers: dict[str, str]
    chunks: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]


async def _single(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def _nothing() -> None:
    return None


@dataclass(repr=False)
class Playback:
    client: Client
    event_id: str
    url: str
    content_type: str = HLS_TYPE
    created: float = field(default_factory=lambda: time.monotonic())
    expires_at: float = field(default_factory=lambda: time.monotonic() + PLAYBACK_TTL)
    resources: dict[str, str] = field(default_factory=dict)
    _probed: tuple[float, Response, str] | None = None

    def __post_init__(self) -> None:
        self.resources["0"] = self.url

    @property
    def progressive(self) -> bool:
        return self.content_type == MP4_TYPE

    def _use(self, key: str, byte_range: str | None) -> str:
        now = time.monotonic()
        if now >= self.expires_at or key not in self.resources:
            raise AccessDenied("Recording playback expired; select the recording again")
        if byte_range is not None and not re.fullmatch(
            r"bytes=[0-9]{1,15}-[0-9]{0,15}", byte_range
        ):
            raise ProtocolError("Invalid recording byte range")
        # Sliding: a paused player can resume, within the absolute bound.
        self.expires_at = min(now + PLAYBACK_TTL, self.created + PLAYBACK_MAX)
        return self.resources[key]

    def register(self, url: str) -> str:
        url = validate_media_url(url, self.event_id)
        for key, existing in self.resources.items():
            if existing == url:
                return key
        if len(self.resources) >= 512:
            raise _TooManyResources("Recording contains too many media resources")
        key = str(len(self.resources))
        self.resources[key] = url
        return key

    async def probe(self) -> None:
        """Fetch the top playlist once at resolve time; the player's first read reuses it."""
        response, url = await self.fetch("0")
        if not response.body.lstrip(b"\xef\xbb\xbf").startswith(b"#EXTM3U"):
            raise ProtocolError("Recording HLS playlist is invalid")
        self._probed = time.monotonic(), response, url

    async def fetch(self, key: str, byte_range: str | None = None) -> tuple[Response, str]:
        url = self._use(key, byte_range)
        if key == "0" and byte_range is None and (probed := self._probed) is not None:
            self._probed = None
            if time.monotonic() - probed[0] < PROBE_TTL:
                return probed[1], probed[2]
        return await fetch_media(
            self.client, url, self.event_id, {"Range": byte_range} if byte_range else None
        )

    async def open_stream(self, key: str, byte_range: str | None = None) -> MediaStream:
        """Stream a progressive resource without buffering it whole."""
        url = self._use(key, byte_range)
        http = self.client.http
        if not isinstance(http, Http):
            response, _ = await fetch_media(
                self.client, url, self.event_id, {"Range": byte_range} if byte_range else None
            )
            return MediaStream(response.status, response.headers, _single(response.body), _nothing)
        for _ in range(4):
            validate_media_url(url, self.event_id)
            headers = await _media_headers(
                self.client, url, {"Range": byte_range} if byte_range else {}
            )
            upstream = await _open(http, url, headers)
            if upstream.status == 401 and "Authorization" in headers:
                upstream.close()
                token = await self.client.auth.access_token(
                    rejected_token=headers["Authorization"].removeprefix("Bearer ")
                )
                headers["Authorization"] = "Bearer " + token
                upstream = await _open(http, url, headers)
            if upstream.status in REDIRECTS:
                location = upstream.headers.get("Location")
                upstream.close()
                if not location:
                    raise ProtocolError("Recording redirect did not contain a destination")
                url = _redirected(url, location)
                continue
            try:
                check_status(Response(upstream.status, dict(upstream.headers), b""))
            except MyQError:
                upstream.close()
                raise

            async def close(upstream: aiohttp.ClientResponse = upstream) -> None:
                upstream.close()

            return MediaStream(upstream.status, dict(upstream.headers), _chunks(upstream), close)
        raise ProtocolError("Recording exceeded the redirect limit")

    def rewrite_manifest(self, body: bytes, base: str, link) -> bytes:
        """Keep every playlist, segment, initialization map and key behind HA.

        An unusable rendition or segment URI is dropped with its own tags rather than
        failing the whole playlist; keys and initialization maps still fail closed.
        """
        try:
            if len(body) > 1024 * 1024:
                raise ValueError
            manifest = body.decode("utf-8-sig")
            if not manifest.startswith("#EXTM3U"):
                raise ValueError
            lines: list[str] = []
            kept = dropped = 0
            for line in manifest.splitlines():
                # A whitespace-only line is not an address (it would resolve to the
                # playlist itself).
                if not line.strip() or line.startswith("#"):
                    if "URI=" in line:
                        expected = line.count("URI=")
                        try:
                            line, count = re.subn(
                                r'URI="([^"\r\n]+)"',
                                lambda match: (
                                    'URI="' + link(self.register(urljoin(base, match[1]))) + '"'
                                ),
                                line,
                            )
                        except _TooManyResources:
                            raise ValueError from None
                        except ProtocolError:
                            if line.startswith(("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")):
                                dropped += 1
                                continue  # An optional rendition.
                            raise ValueError from None
                        if count != expected:
                            raise ValueError
                    lines.append(line)
                    continue
                try:
                    lines.append(link(self.register(urljoin(base, line.strip()))))
                    kept += 1
                except _TooManyResources:
                    # Dropping the rest would play as a complete, shorter recording.
                    raise ValueError from None
                except ProtocolError:
                    dropped += 1
                    # Drop the tags that described only this URI.
                    while lines and lines[-1].startswith(
                        ("#EXTINF", "#EXT-X-STREAM-INF", "#EXT-X-BYTERANGE")
                    ):
                        lines.pop()
            if dropped and not kept:
                raise ValueError
            return ("\n".join(lines) + "\n").encode()
        except (ValueError, UnicodeError):
            raise ProtocolError("Invalid recording HLS playlist") from None


async def _open(http: Http, url: str, headers: dict[str, str]) -> aiohttp.ClientResponse:
    http.backoff.check(url)
    try:
        response = await http.session.request(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT, **headers},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=TIMEOUT, sock_read=TIMEOUT),
        )
    except TimeoutError:
        raise CannotConnect("Recording media did not respond in time") from None
    except aiohttp.ClientError as err:
        # aiohttp messages may contain signed URLs.
        raise CannotConnect(f"Recording media connection failed ({type(err).__name__})") from None
    if response.status == 429:
        delay = retry_seconds(response.headers.get("Retry-After"))
        error = RateLimited(delay, server_retry_after="Retry-After" in response.headers)
        response.close()
        http.backoff.record(url, error)
        raise error
    return response


async def _chunks(response: aiohttp.ClientResponse) -> AsyncIterator[bytes]:
    total = 0
    try:
        async for chunk in response.content.iter_chunked(65536):
            total += len(chunk)
            if total > MAX_STREAM:
                raise ProtocolError("Recording exceeded the size limit")
            yield chunk
    except TimeoutError:
        raise CannotConnect("Recording media stopped responding") from None
    except aiohttp.ClientError as err:
        raise CannotConnect(f"Recording media connection failed ({type(err).__name__})") from None
    finally:
        response.close()

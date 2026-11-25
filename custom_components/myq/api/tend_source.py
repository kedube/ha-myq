"""Serve a TC camera's video and sound to Home Assistant as a local stream source.

Home Assistant's own players read an MPEG transport stream from this address and
repackage it for browsers (WebRTC or HLS): the camera's H.264 as it was sent, and its
microphone as Opus (the camera's own, or its G.711 re-encoded). Video is never decoded
or re-encoded, so the picture is the camera's own; timestamps are arrival times, as a
player would give bare H.264.

A reader learns which streams there are from the tables it reads first, and go2rtc
never reads them again. So while the camera's audio connection is still opening, the
tables already list the audio, and the audio carries silence until the camera's sound
begins; a reader never waits for it.

The server listens on loopback only, under an unguessable path, and carries media
only: the MyQ token and Tend session material never leave the camera session. One
camera session is shared by every reader and ends shortly after the last one leaves.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiohttp import web

from .errors import CannotConnect, MyQError
from .live_audio import CODEC_NAMES, LiveAudio
from .mpegts import CLOCK, TsMuxer

_LOGGER = logging.getLogger(__name__)

# The camera stops this long after its last reader leaves, so a player that reconnects
# straight away (a page reload, a second card) reuses the open session.
IDLE_SECONDS = 15
# Login, a possible wake-up, and each video mode in turn: two UDP rendezvous attempts,
# Tend's UDP relay and its TLS video channel, each awaiting the camera's acceptance.
START_TIMEOUT = 150
# After a failed start or a session that ended on its own, readers are refused for this
# long instead of each one opening another camera login: Home Assistant's players retry
# on their own, about once a second.
RETRY_SECONDS = 15
WRITE_TIMEOUT = 30
# A session that has given its readers no keyframe after this long is ended and says
# why. Shorter than the 30 seconds Home Assistant's stream worker waits for data.
KEYFRAME_SECONDS = 25
# Chunks one reader may fall behind before it restarts at a keyframe: about 15 seconds
# of pictures (20 a second) and sound (50 a second).
MAX_PENDING = 1024
MAX_PENDING_BYTES = 16 * 1024 * 1024  # The same limit by size: records vary widely.
_START = b"\x00\x00\x00\x01"
_NEXT = b"\x00\x00\x01"


class VideoSession(Protocol):
    """What the source needs from a camera session.

    Its owner hands it the source's video and audio callbacks before it starts. A
    session that asks for the camera's audio says so with ``audio_pending`` until the
    audio arrives or fails.
    """

    async def wait_closed(self) -> None: ...

    async def close(self) -> None: ...


def nal_units(record: bytes) -> list[bytes]:
    """The NAL units of an Annex-B record, without start codes or trailing zero bytes."""
    units = []
    start = record.find(b"\x00\x00\x01")
    while start != -1:
        start += 3
        end = record.find(b"\x00\x00\x01", start)
        unit = (record[start:] if end == -1 else record[start:end]).rstrip(b"\x00")
        if unit:
            units.append(unit)
        start = end
    return units


def _first_ue(data: bytes) -> int:
    """The Exp-Golomb value that opens ``data``: a parameter set's own identifier."""
    bits = int.from_bytes(data[:4].ljust(4, b"\x00"), "big")
    zeros = 32 - bits.bit_length()
    return (bits >> (31 - 2 * zeros)) - 1 if zeros < 16 else 0


def _observed(task: asyncio.Future[Any]) -> None:
    """Mark a background task's outcome as seen; its waiters report it themselves."""
    if not task.cancelled():
        task.exception()


class _Reader:
    """One local consumer; its stream always begins at a keyframe."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.size = 0
        self.started = False
        self.begun = False  # Its stream has begun, at least once.
        self.ended = False

    def put(self, data: bytes, keyframe: bool) -> bool:
        """Queue one chunk; True when it is the first of this reader's stream."""
        if self.ended:
            return False
        if self.queue.qsize() >= MAX_PENDING or self.size + len(data) > MAX_PENDING_BYTES:
            # A reader this far behind restarts at a keyframe: skipping records would
            # hand its decoder a broken picture instead.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.size = 0
            self.started = False
        if keyframe:
            self.started = True
        if self.started:
            self.queue.put_nowait(data)
            self.size += len(data)
        first = self.started and not self.begun
        self.begun = self.begun or self.started
        return first

    async def get(self) -> bytes:
        data = await self.queue.get()
        # Not below zero: the queue may have been emptied while this one was handed over.
        self.size = max(0, self.size - len(data))
        return data

    def end(self) -> None:
        if not self.ended:
            self.ended = True
            self.queue.put_nowait(b"")


AudioCallback = Callable[[int, bytes, int | None], None]


class TendSource:
    """A loopback MPEG-TS address backed by one shared camera session."""

    def __init__(
        self,
        open_session: Callable[[Callable[[bytes], None], AudioCallback], Awaitable[Any]],
        session_closed: Callable[[Any], Awaitable[None]],
    ) -> None:
        self._open_session = open_session
        self._session_closed = session_closed
        self._runner: web.AppRunner | None = None
        self._url: str | None = None
        self._server_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._session: VideoSession | None = None
        self._start: asyncio.Future[None] | None = None
        self._watch: asyncio.Task | None = None
        self._readers: set[_Reader] = set()
        # Parameter sets by their own identifier: a camera may use more than one.
        self._sps: dict[int, bytes] = {}
        self._pps: dict[int, bytes] = {}
        # The session's transport stream: one clock, one writer, the audio it carries.
        self._origin = time.monotonic()
        self._mux = TsMuxer()
        self._audio = LiveAudio()
        self._opened = 0.0  # When the session opened, to time the start of its sound.
        self._video_pts: int | None = None
        self._picture_sliced = False  # The open picture already holds a slice.
        self._idle: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task | None = None
        self._shutdown: asyncio.Task | None = None
        self._closed = False
        self._retry_at = 0.0
        # Diagnostics: counts and the integration's own error text only.
        self.sessions = 0
        self.records = 0
        self.unframed = 0
        self.keyframes = 0
        self.audio_packets = 0
        self.audio_codec: str | None = None
        self.audio_started_after: float | None = None
        self.readers_started = 0
        self.readers_with_audio = 0
        self.last_error: str | None = None
        # How the latest session reached the camera (modes tried, flags), set by its owner.
        self.last_transport: dict[str, Any] | None = None

    @property
    def session(self) -> Any:
        return self._session

    @property
    def readers(self) -> int:
        return len(self._readers)

    def status(self) -> dict[str, Any]:
        """Counts and this integration's own error text; no addresses or identifiers."""
        media = getattr(self._session, "media", None)
        transport = getattr(self._session, "transport", None)
        return {
            "address_served": self._url is not None,
            "session_open": self._session is not None,
            "starting": self._start is not None,
            "readers": len(self._readers),
            "sessions_started": self.sessions,
            "records": self.records,
            # Records that were not H.264 with start codes, so could not be served.
            "unframed_records": self.unframed,
            "keyframes": self.keyframes,
            # Readers whose stream began, and those whose first tables listed audio.
            "readers_started": self.readers_started,
            "readers_with_audio": self.readers_with_audio,
            # Opus packets of the camera's sound served, the codec it arrived in, and
            # seconds from the latest session opening to its first sound.
            "audio_packets": self.audio_packets,
            "audio_codec": self.audio_codec,
            "audio_dropped": self._audio.dropped,
            "audio_started_after": self.audio_started_after,
            "media_path": getattr(media, "path", None),
            "camera_packets": getattr(media, "packets", None),
            "unreadable_packets": getattr(media, "unreadable", None),
            "transport": transport if isinstance(transport, dict) else self.last_transport,
            "last_error": self.last_error,
        }

    async def url(self) -> str:
        """The stream address, starting the loopback server on first use."""
        async with self._server_lock:
            if self._closed:
                raise CannotConnect("The camera stream is closed")
            if self._url is None:
                path = "/" + secrets.token_urlsafe(24) + ".ts"
                app = web.Application()
                # GET only: a HEAD request must not open the camera.
                app.router.add_get(path, self._serve, allow_head=False)
                # A reader that disconnects cancels its handler; shutdown does not wait.
                runner = web.AppRunner(
                    app, access_log=None, handler_cancellation=True, shutdown_timeout=0.1
                )
                await runner.setup()
                try:
                    await web.TCPSite(runner, "127.0.0.1", 0).start()
                    # The port the system assigned; the site itself only knows "0".
                    port = runner.addresses[0][1]
                except BaseException:
                    await runner.cleanup()
                    raise
                self._runner, self._url = runner, f"http://127.0.0.1:{port}{path}"
            return self._url

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        try:
            reader = await self._subscribe()
        except MyQError as error:
            raise web.HTTPServiceUnavailable(text=str(error)) from None
        response = web.StreamResponse(
            headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"}
        )
        try:
            await response.prepare(request)
            while data := await reader.get():
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await response.write(data)
        except ConnectionError:
            pass  # The reader went away.
        except TimeoutError:
            # The reader stopped reading: drop the connection instead of waiting on it.
            if request.transport is not None:
                request.transport.abort()
        finally:
            self._unsubscribe(reader)
        return response

    async def _subscribe(self) -> _Reader:
        """Join the camera session, starting one when there is none.

        The start belongs to the source, not to the reader that asked for it: a player
        that gives up waiting (Home Assistant's stream worker does after 30 seconds)
        leaves the start running, and its retry joins the same one.
        """
        while self._session is None:
            if self._closed:
                raise CannotConnect("The camera stream is closed")
            start = self._start
            if start is None:
                if time.monotonic() < self._retry_at:
                    raise CannotConnect(self.last_error or "The camera stream is unavailable")
                start = self._start = asyncio.ensure_future(self._begin())
                start.add_done_callback(_observed)
            try:
                await asyncio.shield(start)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise  # This reader went away.
                raise CannotConnect("The camera stream was stopped") from None
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        reader = _Reader()
        self._readers.add(reader)
        return reader

    async def _begin(self) -> None:
        """Open the camera session and take ownership of it, or record why not."""
        try:
            async with self._session_lock:
                # A new session's stream: its clock, continuity counters and audio.
                self._origin = time.monotonic()
                self._mux, self._audio, self.audio_started_after = TsMuxer(), LiveAudio(), None
                self._video_pts, self._picture_sliced = None, False
                try:
                    async with asyncio.timeout(START_TIMEOUT):
                        # Awaited directly: a cancellation lands inside the opener,
                        # which then cleans up after itself; no result can be lost.
                        session = await self._open_session(self._record, self._audio_frame)
                except TimeoutError:
                    message = "The camera did not start sending video in time"
                    self._failed(message)
                    raise CannotConnect(message) from None
                except MyQError as error:
                    self._failed(str(error))
                    raise
                except Exception:
                    # A defect, not a camera answer: it still must not be retried by
                    # every player once a second.
                    _LOGGER.exception("Unexpected error while starting camera video")
                    message = "The camera stream could not start"
                    self._failed(message)
                    raise CannotConnect(message) from None
                self.sessions += 1
                self.last_error = None
                self._session = session
                self._opened = time.monotonic()
                self._watch = asyncio.create_task(self._watch_session(session))
                if not self._readers:
                    # Whoever asked has left; keep it for their retry, briefly.
                    self._arm_idle()
        finally:
            if self._start is asyncio.current_task():
                self._start = None

    def _failed(self, message: str) -> None:
        self.last_error = message
        self._retry_at = time.monotonic() + RETRY_SECONDS

    def _unsubscribe(self, reader: _Reader) -> None:
        reader.ended = True
        self._readers.discard(reader)
        if not self._readers and self._session is not None and not self._closed:
            self._arm_idle()

    def _arm_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(IDLE_SECONDS, self._idle_elapsed)

    def _idle_elapsed(self) -> None:
        self._idle = None
        self._idle_task = asyncio.create_task(self._close_if_idle())

    async def _close_if_idle(self) -> None:
        async with self._session_lock:
            if not self._readers:
                await self._close_session()

    def _clock(self) -> int:
        """The session's 90 kHz clock, from one second, so no time is ever negative."""
        return int((time.monotonic() - self._origin) * CLOCK) + CLOCK

    def _audio_pending(self) -> bool:
        """The session asked for the camera's audio, which has neither begun nor failed."""
        return bool(getattr(self._session, "audio_pending", False))

    def _audio_frame(self, codec: int, data: bytes, sequence: int | None) -> None:
        """Hand one camera audio frame to every reader, as Opus."""
        placed = self._audio.packets(codec, data, sequence, self._clock())
        if not placed:
            return
        if self.audio_started_after is None:
            self.audio_started_after = round(max(0.0, time.monotonic() - self._opened), 1)
        self.audio_codec = CODEC_NAMES.get(codec)
        self.audio_packets += len(placed)
        chunk = b"".join(self._mux.audio(pts, packet) for pts, packet in placed)
        for reader in self._readers:
            reader.put(chunk, False)

    def _record(self, record: bytes) -> None:
        """Hand one camera record to every reader as a transport stream access unit.

        As FFmpeg writes H.264: each keyframe is led by the current SPS and PPS, a
        picture's first slice and the parameter sets get 4-byte start codes, and any
        further slice of the same picture gets a 3-byte one. Parameter sets the camera
        sends on their own are held and sent with the keyframe they describe.

        A picture is one PES. A new one begins with each picture's first slice, or with
        what leads it (an SEI, an access unit delimiter), once the open one holds a
        slice; further slices join the open one. Each keyframe begins a PES of its own
        behind the stream's tables, so a reader can begin there.

        The tables list the audio once the camera's sound is carried, or while it is
        still on its way; until it arrives, each record brings the silence since the
        last one.
        """
        units = nal_units(record)
        if not units:
            self.unframed += 1
            return
        self.records += 1
        body = []
        keyframe = sliced = False
        for unit in units:
            kind = unit[0] & 0x1F
            sliced = sliced or kind in (1, 5)
            if kind == 7:
                # After the NAL header: profile, constraint flags and level, then the id.
                if (identifier := _first_ue(unit[4:])) < 32:
                    self._sps[identifier] = unit
            elif kind == 8:
                if (identifier := _first_ue(unit[1:])) < 256:
                    self._pps[identifier] = unit
            elif kind in (1, 5) and not (len(unit) > 1 and unit[1] & 0x80):
                # A slice's header opens with first_mb_in_slice, coded as a single 1 bit
                # when it is zero. Anything else continues the picture already begun.
                body.append(_NEXT + unit)
            else:
                body.append(_START + unit)
                keyframe = keyframe or kind == 5
        if not body:
            return
        if keyframe:
            if not self._sps or not self._pps:
                return  # Not decodable by anyone yet.
            self.keyframes += 1
            body = [
                *(_START + self._sps[key] for key in sorted(self._sps)),
                *(_START + self._pps[key] for key in sorted(self._pps)),
                *body,
            ]
        data = b"".join(body)
        begins = body[0].startswith(_START) and self._picture_sliced
        if keyframe or begins or self._video_pts is None:
            # Arrival time, after the last picture's even when they arrive together.
            last = self._video_pts
            self._video_pts = self._clock() if last is None else max(self._clock(), last + 1)
            chunk = self._mux.video(self._video_pts, data, keyframe)
            self._picture_sliced = sliced
        else:
            chunk = self._mux.video_continued(data)  # More of the open picture.
            self._picture_sliced = self._picture_sliced or sliced
        pending = self._audio_pending()
        audio = bool(self._audio.frames) or pending
        if keyframe:
            chunk = self._mux.tables(audio, self._audio.channels) + chunk
        if pending:
            silence = self._audio.silence(self._clock())
            chunk += b"".join(self._mux.audio(pts, packet) for pts, packet in silence)
        for reader in self._readers:
            if reader.put(chunk, keyframe):
                self.readers_started += 1
                self.readers_with_audio += audio

    async def _watch_session(self, session: VideoSession) -> None:
        """End every reader when the camera session ends, or never becomes playable."""
        before = self.keyframes
        ended = asyncio.ensure_future(session.wait_closed())
        message = None
        try:
            while not ended.done():
                if self.keyframes != before:
                    await asyncio.wait({ended})
                    break
                await asyncio.wait({ended}, timeout=KEYFRAME_SECONDS)
                if not ended.done() and self._readers and self.keyframes == before:
                    # Packets may be arriving, yet nothing a player could start from:
                    # without this the readers would wait on an open, silent stream.
                    message = "The camera sent no playable keyframe"
                    break
            if message is None:
                try:
                    ended.result()
                    message = "The camera session ended"
                except MyQError as error:
                    message = str(error)
        finally:
            if not ended.done():
                ended.cancel()
        if self._session is session and not self._closed:
            # Players reconnect at once; the pause keeps a session that keeps ending
            # (a camera going offline, a second login elsewhere) from re-logging in
            # every second.
            self._failed(message)
            async with self._session_lock:
                if self._session is session:
                    await self._close_session(watcher=True)

    async def _close_session(self, *, watcher: bool = False) -> None:
        session, self._session = self._session, None
        watch, self._watch = self._watch, None
        self._sps, self._pps = {}, {}
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        # However a session ends, its readers end with it.
        for reader in self._readers:
            reader.end()
        if session is None:
            return
        if watch is not None and not watcher:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        try:
            await session.close()
        finally:
            await self._session_closed(session)

    async def stop(self, *, pause: float = 0) -> None:
        """End the camera session and its readers now; the address stays valid.

        ``pause`` refuses readers for that many seconds afterwards, so a player that
        reconnects at once does not reopen the camera while it is being turned off.
        """
        if pause:
            self._retry_at = max(self._retry_at, time.monotonic() + pause)
        # A start in flight holds the session lock for as long as the camera takes:
        # end it, and with it the wait of every reader queued behind it.
        if (start := self._start) is not None:
            start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        async with self._session_lock:
            await self._close_session()

    async def close(self) -> None:
        """Stop for good; completes even when the caller is cancelled."""
        if self._shutdown is None:
            self._closed = True
            self._shutdown = asyncio.create_task(self._close())
        await asyncio.shield(self._shutdown)

    async def _close(self) -> None:
        try:
            await self.stop()
            if self._idle_task is not None:
                await asyncio.gather(self._idle_task, return_exceptions=True)
        finally:
            async with self._server_lock:
                runner, self._runner, self._url = self._runner, None, None
                if runner is not None:
                    await runner.cleanup()

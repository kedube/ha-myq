"""Receive a TC camera's video through Tend's TLS video channel (mode 0, "TCP-SR").

The app's last resort when UDP does not get through (MediaConnectionManager): the
broker relays the camera's video over its own TLS connection. The login's answer names
the channel; it is joined with the login's session identity, and the camera's H.264
arrives in framed records. TLS is the only encryption: the records carry no AES layer.
Addresses and session material stay inside the receiver and never reach logs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import ssl
import struct
import time
from collections.abc import Callable

from .errors import CannotConnect, MyQError, ProtocolError
from .tend import HOST, Frame, _client_tls_context, trusted_host

CONNECT_TIMEOUT = 8.0  # The app allows 3 s; a slow host gets a little longer here.
FRAME_TIMEOUT = 30.0  # The first record, and then any gap between records.
KEEPALIVE_INTERVAL = 60
CHANNEL_SERVICE = 122
JOIN_VIDEO = 508
JOIN_AUDIO = 509
_ACK = 257
_MEDIA = (413, 414)
_LOGOUT = 408
_KEEPALIVE = 526
_DEFAULT_PORT = "5101"
# OpenSSL's host name and IP address mismatch codes: the channel may present the
# front door's certificate, as the brokers do.
_NAME_MISMATCH = (62, 64)
_PEER = tuple[str, int]


def _channel_host(host: str) -> bool:
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return trusted_host(host)
    return address.is_global


def channel_address(login: dict[str, str], kind: str) -> _PEER | None:
    """The TLS address of the login's ``video`` or ``audio`` channel (eg1/b.n()).

    ``<kind>-channel-host`` with ``<kind>-channel-port-tls``, failing that the lines
    ``socket,<host>,<port>,<TLS port>`` of ``<kind>-hostname`` or ``<kind>-host``.
    Only hosts within Tend's domains, or public addresses, are used.
    """
    found = []
    host = login.get(f"{kind}-channel-host", "").strip().lower()
    port = login.get(f"{kind}-channel-port-tls", "").strip()
    if host and port:
        found.append((host, port))
    for key in (f"{kind}-hostname", f"{kind}-host"):
        for line in re.split(r"\r\n|\r|\n", login.get(key, "")):
            fields = [field.strip() for field in line.split(",")]
            if len(fields) >= 3 and fields[0].lower() == "socket" and fields[1]:
                tls = fields[3] if len(fields) > 3 and fields[3] else _DEFAULT_PORT
                found.append((fields[1].lower(), tls))
    for host, port in found:
        if (
            port.isascii()
            and port.isdigit()
            and 1 <= int(port) <= 65535
            and len(host) <= 253
            and _channel_host(host)
        ):
            return host, int(port)
    return None


def channel_records(data: bytes, sources: frozenset[str]) -> list[bytes] | None:
    """The H.264 records of one video-channel message, or None when it is not video.

    0x05, a byte, the source's alias (big-endian u32 length), then a big-endian u32
    length of the rest: four header bytes (codec, size index, channel, key frame bit)
    and records, each a little-endian u32 length and that many bytes
    (VideoConnectionManager.handleServerRelayData).
    """
    if len(data) < 10 or data[0] != 5:
        return None
    source_length = int.from_bytes(data[2:6], "big")
    if 10 + source_length > len(data):
        return None
    source = data[6 : 6 + source_length].decode("utf-8", "replace").lower()
    size = int.from_bytes(data[6 + source_length : 10 + source_length], "big")
    if size < 4 or 10 + source_length + size != len(data) or source not in sources:
        return None
    records = []
    cursor = 14 + source_length
    while cursor + 4 <= len(data):
        length = int.from_bytes(data[cursor : cursor + 4], "little")
        cursor += 4
        if not length or cursor + length > len(data):
            break
        records.append(data[cursor : cursor + length])
        cursor += length
    return records


def channel_audio(data: bytes, sources: frozenset[str]) -> tuple[int, bytes] | None:
    """The codec and audio of one audio-channel message, or None when it is not audio.

    0x0B, a byte, the source's alias (big-endian u32 length), then a big-endian u32
    length of the rest: the codec byte (low five bits) and three more, a little-endian
    u32 length, four bytes, and the audio
    (AudioConnectionManager.handleServerRelayData).
    """
    if len(data) < 10 or data[0] != 0x0B:
        return None
    source_length = int.from_bytes(data[2:6], "big")
    if 10 + source_length > len(data):
        return None
    source = data[6 : 6 + source_length].decode("utf-8", "replace").lower()
    size = int.from_bytes(data[6 + source_length : 10 + source_length], "big")
    if size <= 12 or 10 + source_length + size != len(data) or source not in sources:
        return None
    combined = int.from_bytes(data[14 + source_length : 18 + source_length], "little")
    if combined <= 4 or size - 4 not in (combined + 4, combined + 8):
        return None
    return data[10 + source_length] & 0x1F, data[22 + source_length :]


class TendChannel:
    """One TLS video (or audio) channel joined to a logged-in Tend session."""

    def __init__(
        self,
        on_record: Callable[[bytes], None],
        on_end: Callable[[CannotConnect], None] | None = None,
        *,
        on_audio: Callable[[int, bytes, int | None], None] | None = None,
    ) -> None:
        self._on_record = on_record
        self._on_end = on_end
        # The camera's audio frames on an audio channel: codec, audio, no sequence.
        self._on_audio = on_audio
        self._audio = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._sender = b"\0" * 16
        self._recipient = 0
        self._sources: frozenset[str] = frozenset()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._ended = False
        self._write_lock = asyncio.Lock()
        self._last_video_at = 0.0
        self._watch_silence = True
        # Counters for diagnostics: video messages received, and those without video.
        self.packets = 0
        self.unreadable = 0

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def path(self) -> str | None:
        return "tcp_relay" if self._started and not self._ended else None

    async def _open(self, host: str, port: int) -> None:
        context = await _client_tls_context()
        # Its own name first, then the front door's, whose certificate Tend's brokers
        # present; the chain is verified either way.
        for name in dict.fromkeys((host, HOST)):
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    host, port, ssl=context, server_hostname=name
                )
                return
            except ssl.SSLCertVerificationError as error:
                if error.verify_code not in _NAME_MISMATCH:
                    break
        raise CannotConnect("Tend video channel certificate was not accepted")

    async def _send(self, action: int) -> None:
        async with self._write_lock:
            if self._writer is None:
                raise CannotConnect("Tend video channel is not connected")
            frame = Frame(action, self._sender, self._recipient, service=CHANNEL_SERVICE)
            self._writer.write(frame.encode())
            await self._writer.drain()

    async def start(
        self,
        address: _PEER,
        sender: bytes,
        recipient: int,
        sources: set[str],
        *,
        join: int = JOIN_VIDEO,
    ) -> None:
        """Connect, join with the login's identity, and start reading records.

        ``sources`` are the aliases the camera's records may name (case-insensitive).
        """
        if self._started or self._ended:
            raise ProtocolError("A Tend video channel cannot be reused")
        if len(sender) != 16 or join not in (JOIN_VIDEO, JOIN_AUDIO):
            raise ProtocolError("Invalid Tend video channel registration")
        self._started = True
        # An audio channel carries the camera's microphone and the client's talkback,
        # not video: it is not ended for going quiet.
        self._audio = join == JOIN_AUDIO
        self._watch_silence = not self._audio
        self._sender, self._recipient = sender, recipient
        self._sources = frozenset(source.lower() for source in sources if source)
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await self._open(*address)
                assert self._reader is not None
                await self._send(join)
                # eg1/b.A(): any answer but action 1 accepts the channel.
                if (await Frame.read(self._reader)).action == 1:
                    raise CannotConnect("Tend refused the video channel")
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            await self.close()
            raise CannotConnect("Tend video channel connection failed or timed out") from None
        except MyQError:
            await self.close()
            raise
        self._last_video_at = time.monotonic()
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._maintain())]

    async def write(self, data: bytes) -> None:
        """Raw bytes on the channel: the app's talkback over an audio channel."""
        async with self._write_lock:
            if self._writer is None or self._ended:
                raise CannotConnect("Tend audio channel is not connected")
            self._writer.write(data)
            await self._writer.drain()

    async def _read(self) -> None:
        assert self._reader is not None
        try:
            while True:
                frame = await Frame.read(self._reader)
                if frame.action == _ACK or frame.action not in _MEDIA:
                    continue
                self.packets += 1
                if self._audio:
                    found = channel_audio(frame.data, self._sources)
                    if found is None:
                        self.unreadable += 1
                    elif self._on_audio is not None:
                        self._on_audio(*found, None)
                    continue
                records = channel_records(frame.data, self._sources)
                if not records:
                    self.unreadable += 1
                    continue
                self._last_video_at = time.monotonic()
                for record in records:
                    if self._ended:
                        return
                    self._on_record(record)
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError, MyQError):
            self._finish(CannotConnect("Tend video channel disconnected"))

    async def _maintain(self) -> None:
        """Keepalives every minute; a channel silent for too long ends."""
        sent = time.monotonic()
        try:
            while not self._ended:
                await asyncio.sleep(2)
                now = time.monotonic()
                if self._watch_silence and now - self._last_video_at > FRAME_TIMEOUT:
                    self._finish(CannotConnect("Tend camera stopped sending video"))
                    return
                if now - sent >= KEEPALIVE_INTERVAL:
                    sent = now
                    await self._send(_KEEPALIVE)
        except asyncio.CancelledError:
            raise
        except (OSError, MyQError):
            self._finish(CannotConnect("Tend video channel disconnected"))

    def _finish(self, error: CannotConnect | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        if error is not None and self._on_end is not None:
            self._on_end(error)

    async def close(self) -> None:
        """Log the channel out and close it, including during setup."""
        self._finish()
        tasks, self._tasks = self._tasks, []
        current = asyncio.current_task()
        for task in tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not current), return_exceptions=True
        )
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            async with asyncio.timeout(2):
                frame = Frame(
                    _LOGOUT,
                    self._sender,
                    self._recipient,
                    struct.pack("<I", 0),
                    service=CHANNEL_SERVICE,
                )
                writer.write(frame.encode())
                await writer.drain()
        except (OSError, TimeoutError, MyQError):
            pass
        writer.close()
        try:
            async with asyncio.timeout(2):
                await writer.wait_closed()
        except (OSError, TimeoutError):
            pass

"""Receive the mode-9 Tend camera stream over authenticated UDP candidates.

The broker supplies the media identity, candidate relay and optional encryption
key. None of those values are logged. This module only yields encoded H.264;
camera discovery and signaling remain with the existing Tend connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import struct
import time
from collections import deque

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .errors import CannotConnect, ProtocolError

CONNECT_TIMEOUT = 8.0
FRAME_TIMEOUT = 30.0
MAX_PACKET = 2 * 1024 * 1024
MAX_FRAGMENTS = 4096
MAX_QUEUED_BYTES = 4 * 1024 * 1024
_MAGIC = b"SDNK"
_PEER = tuple[str, int]


def _setup_packet(kind: int, body: bytes) -> bytes:
    return struct.pack("!4sBBBB8sI", _MAGIC, 1, 0, 0, kind, bytes(8), len(body)) + body


def _rtp_body(packet: bytes) -> bytes | None:
    """Respect RTP CSRCs, extensions and padding before reading the camera payload."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return None
    start = 12 + (packet[0] & 15) * 4
    end = len(packet)
    if start > end:
        return None
    if packet[0] & 16:
        if start + 4 > end:
            return None
        start += 4 + int.from_bytes(packet[start + 2 : start + 4], "big") * 4
    if packet[0] & 32:
        padding = packet[-1]
        if not padding or padding > end - start:
            return None
        end -= padding
    return packet[start:end] if start <= end else None


def _video_records(payload: bytes, key: bytes) -> list[bytes]:
    """Decode an entire bounded camera record envelope or discard it atomically."""
    if not 10 <= len(payload) <= MAX_PACKET or payload[:2] != b"w1":
        return []
    if int.from_bytes(payload[2:6], "little") != len(payload) - 6:
        return []
    body = payload[10:]
    if payload[6] >> 5 == 1:
        if len(key) != 16 or len(body) < 32 or len(body) % 16:
            return []
        try:
            decoder = Cipher(algorithms.AES(key), modes.CBC(body[:16])).decryptor()
            padded = decoder.update(body[16:]) + decoder.finalize()
            unpad = PKCS7(128).unpadder()
            body = unpad.update(padded) + unpad.finalize()
        except ValueError:
            return []
    result = []
    cursor = 0
    while cursor < len(body):
        if len(body) - cursor < 4:
            return []
        size = int.from_bytes(body[cursor : cursor + 4], "little")
        cursor += 4
        if not size or size > len(body) - cursor:
            return []
        record = body[cursor : cursor + size]
        if not record.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
            return []
        result.append(record)
        cursor += size
    return result


class _Port(asyncio.DatagramProtocol):
    def __init__(self, owner: TendRtp, index: int) -> None:
        self.owner = owner
        self.index = index
        self.transport: asyncio.DatagramTransport | None = None
        self.number = 0
        self.candidates: tuple[_PEER, ...] = ()
        self.peer: _PEER | None = None
        self.ready = False

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.number = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data: bytes, addr: _PEER) -> None:
        self.owner._receive(self, data, addr)

    def error_received(self, exc: Exception) -> None:
        # A candidate can refuse an initial probe while another remains reachable.
        # Handshake and media deadlines bound these asynchronous socket errors.
        pass

    def send(self, packet: bytes, peer: _PEER) -> None:
        if self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(packet, peer)


class TendRtp:
    """One bounded video receiver; ``local_ip`` in start is the camera's LAN IP."""

    def __init__(self) -> None:
        self._ports: list[_Port] = []
        self._relay: _PEER = ("", 0)
        self._camera_ip = ""
        self._source_ip = ""
        self._identity = b""
        self._key = b""
        self._started = False
        self._ready = asyncio.Event()
        self._ended = asyncio.Event()
        self._error: CannotConnect | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        self._queued_bytes = 0
        self._maintenance: asyncio.Task | None = None
        self._group: int | None = None
        self._parts: dict[bytes, None] = {}
        self._part_bytes = 0
        self._retired_groups: deque[int] = deque(maxlen=8)
        self._discard_group = False
        self._last_part_at = 0.0
        self._last_video_at = 0.0
        self._ssrc = secrets.randbits(32)
        self._remote_ssrc: int | None = None
        self._first_sequence = 0
        self._highest_sequence = 0
        self._received = 0
        self._prior_expected = 0
        self._prior_received = 0
        self._transit: float | None = None
        self._jitter = 0.0
        self._sender_report = 0
        self._sender_report_at = 0.0

    async def start(self, relay: _PEER, account_alias: str, local_ip: str, aes_key: bytes) -> None:
        """Register the paired UDP ports and complete camera connectivity checks."""
        if self._started or self._ended.is_set():
            raise ProtocolError("A Tend media receiver cannot be reused")
        self._started = True
        try:
            identity = ("V_" + account_alias).encode("ascii")
        except (UnicodeError, TypeError):
            raise ProtocolError("Invalid Tend media identity") from None
        if not account_alias or len(identity) > 258 or b"\0" in identity:
            raise ProtocolError("Invalid Tend media identity")
        if len(aes_key) not in (0, 16) or not 1 <= relay[1] <= 65535:
            raise ProtocolError("Invalid Tend media registration")
        self._identity = identity[:32].ljust(32, b"\0")
        self._key = aes_key
        self._camera_ip = local_ip
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                loop = asyncio.get_running_loop()
                addresses = await loop.getaddrinfo(
                    *relay, family=socket.AF_INET, type=socket.SOCK_DGRAM
                )
                self._relay = (str(addresses[0][4][0]), relay[1])
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                    route.connect(self._relay)
                    self._source_ip = route.getsockname()[0]
                await self._bind()
                await self._handshake()
            self._last_video_at = time.monotonic()
            self._maintenance = asyncio.create_task(self._maintain())
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, TimeoutError):
            await self.close()
            raise CannotConnect("Tend video UDP connection failed or timed out") from None
        except Exception:
            await self.close()
            raise

    async def _bind(self) -> None:
        for _ in range(32):
            sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
            try:
                sockets[0].bind(("0.0.0.0", 0))
                port = sockets[0].getsockname()[1]
                if port == 65535:
                    continue
                sockets[1].bind(("0.0.0.0", port + 1))
                loop = asyncio.get_running_loop()
                for index, sock in enumerate(sockets):
                    endpoint = _Port(self, index)

                    def protocol(endpoint: _Port = endpoint) -> _Port:
                        return endpoint

                    await loop.create_datagram_endpoint(protocol, sock=sock)
                    self._ports.append(endpoint)
                return
            except OSError:
                for endpoint in self._ports:
                    if endpoint.transport is not None:
                        endpoint.transport.close()
                self._ports.clear()
            finally:
                if len(self._ports) != 2:
                    for sock in sockets:
                        sock.close()
        raise OSError("Could not allocate camera UDP ports")

    async def _handshake(self) -> None:
        sequence = 0
        while not self._ready.is_set():
            if self._ended.is_set():
                raise CannotConnect("Tend video UDP receiver was closed")
            for port in self._ports:
                if not port.candidates:
                    registration = (
                        struct.pack(
                            "!BBBBc4s16sH",
                            1,
                            0,
                            0,
                            port.index,
                            b"V",
                            socket.inet_aton(self._source_ip),
                            bytes(16),
                            port.number,
                        )
                        + self._identity * 2
                    )
                    port.send(_setup_packet(1, registration), self._relay)
                elif not port.ready:
                    targets = (port.peer,) if port.peer is not None else port.candidates
                    for target in targets:
                        port.send(
                            _setup_packet(3, bytes([1, sequence]) + self._identity * 2), target
                        )
                        port.send(_setup_packet(4, b"\x01"), target)
            sequence = min(2, sequence + 1)
            try:
                async with asyncio.timeout(0.1):
                    await self._ready.wait()
            except TimeoutError:
                pass

    def _receive(self, port: _Port, data: bytes, remote: _PEER) -> None:
        if self._ended.is_set():
            return
        if data.startswith(_MAGIC):
            if len(data) < 20 or data[4:7] != b"\x01\0\0":
                return
            if int.from_bytes(data[16:20], "big") != len(data) - 20:
                return
            kind = data[7]
            if kind == 2:
                if remote != self._relay or port.candidates or len(data) != 110:
                    return
                candidates = []
                for offset in range(20, 108, 22):
                    address = ipaddress.IPv4Address(data[offset : offset + 4])
                    number = int.from_bytes(data[offset + 20 : offset + 22], "big")
                    # Never choose the receiver itself as the camera. An explicit
                    # loopback relay also permits local protocol fixtures.
                    if str(address) == self._source_ip or address.is_loopback:
                        if not (address.is_loopback and self._relay[0] == self._source_ip):
                            continue
                    if number and not (address.is_unspecified or address.is_multicast):
                        candidate = (str(address), number)
                        if candidate not in candidates:
                            candidates.append(candidate)
                # Prefer the broker's camera LAN address, while retaining announced
                # public candidates for installations that cannot route to that LAN.
                candidates.sort(key=lambda item: item[0] != self._camera_ip)
                port.candidates = tuple(candidates)
                if candidates:
                    port.send(
                        _setup_packet(100, bytes([1, port.index]) + b"V" + self._identity * 2),
                        self._relay,
                    )
                return
            if remote not in port.candidates or (port.peer is not None and remote != port.peer):
                return
            if kind not in (3, 4, 5) or len(data) < 21:
                return
            port.peer = remote
            if kind == 3:
                for sequence in range(3):
                    port.send(_setup_packet(3, bytes([1, sequence]) + self._identity * 2), remote)
            elif kind == 4:
                port.send(_setup_packet(5, data[20:21]), remote)
            else:
                port.ready = True
                if len(self._ports) == 2 and all(endpoint.ready for endpoint in self._ports):
                    self._ready.set()
            return
        if not port.ready or remote != port.peer:
            return
        if port.index:
            self._rtcp(data)
        else:
            self._fragment(data)

    def _fragment(self, data: bytes) -> None:
        if len(data) <= 8:
            return
        now = time.monotonic()
        group = int.from_bytes(data[:4], "little")
        if group in self._retired_groups:
            return
        if self._group != group:
            self._complete_packet()
            if self._ended.is_set():
                return
            if self._group is not None:
                self._retired_groups.append(self._group)
            self._group = group
            self._discard_group = False
        elif now - self._last_part_at > 2:
            self._parts.clear()
            self._part_bytes = 0
            self._discard_group = True
        self._last_part_at = now
        if self._discard_group:
            return
        # The last four header bytes are not established for camera video. Keep
        # arrival order and suppress exact retransmissions without interpreting
        # that word as an index or count. The checks below discard malformed
        # assemblies; reconstructing reordered fragments needs more wire evidence.
        if data in self._parts:
            return
        if len(self._parts) >= MAX_FRAGMENTS or self._part_bytes + len(data) - 8 > MAX_PACKET:
            self._parts.clear()
            self._part_bytes = 0
            self._discard_group = True
            return
        self._parts[data] = None
        self._part_bytes += len(data) - 8

    def _complete_packet(self) -> None:
        packet = b"".join(fragment[8:] for fragment in self._parts)
        self._parts.clear()
        self._part_bytes = 0
        payload = _rtp_body(packet)
        if payload is None:
            return
        records = _video_records(payload, self._key)
        if not records:
            return
        self._observe(packet)
        for record in records:
            if self._queue.full() or self._queued_bytes + len(record) > MAX_QUEUED_BYTES:
                self._finish(CannotConnect("Tend video consumer is not keeping up"))
                return
            self._queue.put_nowait(record)
            self._queued_bytes += len(record)
        self._last_video_at = time.monotonic()

    def _observe(self, packet: bytes) -> None:
        sequence, timestamp, source = struct.unpack_from("!HII", packet, 2)
        if source != self._remote_ssrc:
            self._remote_ssrc = source
            self._first_sequence = self._highest_sequence = sequence
            self._received = self._prior_expected = self._prior_received = 0
            self._transit = None
            self._jitter = 0.0
        else:
            distance = (sequence - (self._highest_sequence & 65535)) & 65535
            if 0 < distance < 32768:
                self._highest_sequence += distance
        self._received += 1
        transit = time.monotonic() * 90000 - timestamp
        if self._transit is not None:
            self._jitter += (abs(transit - self._transit) - self._jitter) / 16
        self._transit = transit

    def _rtcp(self, data: bytes) -> None:
        offset = 0
        while len(data) - offset >= 4:
            size = (int.from_bytes(data[offset + 2 : offset + 4], "big") + 1) * 4
            if data[offset] >> 6 != 2 or size > len(data) - offset:
                return
            if data[offset + 1] == 200 and size >= 28:
                source = int.from_bytes(data[offset + 4 : offset + 8], "big")
                if self._remote_ssrc is None or source == self._remote_ssrc:
                    self._sender_report = int.from_bytes(data[offset + 10 : offset + 14], "big")
                    self._sender_report_at = time.monotonic()
            offset += size

    def _report(self) -> bytes | None:
        if self._remote_ssrc is None:
            return None
        expected = self._highest_sequence - self._first_sequence + 1
        interval = expected - self._prior_expected
        missing = interval - (self._received - self._prior_received)
        fraction = min(255, max(0, missing * 256 // interval)) if interval > 0 else 0
        lost = max(-(1 << 23), min((1 << 23) - 1, expected - self._received))
        self._prior_expected, self._prior_received = expected, self._received
        delay = (
            min(0xFFFFFFFF, int((time.monotonic() - self._sender_report_at) * 65536))
            if self._sender_report_at
            else 0
        )
        return (
            struct.pack("!BBHII", 0x81, 201, 7, self._ssrc, self._remote_ssrc)
            + bytes([fraction])
            + (lost & 0xFFFFFF).to_bytes(3, "big")
            + struct.pack(
                "!IIII",
                self._highest_sequence & 0xFFFFFFFF,
                min(int(self._jitter), 0xFFFFFFFF),
                self._sender_report,
                delay,
            )
        )

    async def _maintain(self) -> None:
        try:
            while not self._ended.is_set():
                await asyncio.sleep(2)
                if time.monotonic() - self._last_video_at > FRAME_TIMEOUT:
                    self._finish(CannotConnect("Tend camera stopped sending video"))
                    return
                report = self._report()
                if report is not None and len(self._ports) == 2:
                    port = self._ports[1]
                    if port.peer is not None:
                        port.send(report, port.peer)
        except asyncio.CancelledError:
            pass

    async def recv(self) -> bytes:
        """Return the next encoded record, with a bounded wait for a usable frame."""
        if self._ended.is_set():
            raise self._error or CannotConnect("Tend video receiver was closed")
        try:
            async with asyncio.timeout(FRAME_TIMEOUT):
                frame = await self._queue.get()
        except TimeoutError:
            error = CannotConnect("Tend camera stopped sending video")
            self._finish(error)
            raise error from None
        if frame is None:
            raise self._error or CannotConnect("Tend video receiver was closed")
        self._queued_bytes -= len(frame)
        return frame

    def _finish(self, error: CannotConnect | None = None) -> None:
        if self._ended.is_set():
            return
        self._error = error
        self._ended.set()
        self._parts.clear()
        self._key = b""
        self._identity = b""
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queued_bytes = 0
        self._queue.put_nowait(None)
        for port in self._ports:
            if port.index and port.peer is not None and port.ready:
                port.send(struct.pack("!BBHI", 0x81, 203, 1, self._ssrc), port.peer)
            if port.transport is not None:
                port.transport.close()

    async def wait_closed(self) -> None:
        await self._ended.wait()
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        """Close both sockets and unblock consumers, including during setup."""
        self._finish()
        if self._maintenance is not None:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
            self._maintenance = None

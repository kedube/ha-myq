"""Tend camera media over UDP: receive the mode-9 video stream, send talkback.

The broker supplies the media identity, a relay and an optional encryption key; none
of those values are logged. What this receiver accepts follows clients known to work
with TC cameras, which are lenient: any setup or media datagram from the relay or from
a camera address the relay announced counts, and a media record is passed on as the
camera sent it. Records are the camera's encoded H.264; nothing is decoded here.

The same rendezvous carries the app's talkback connection, with kind "A" in place of
"V" (seedonk_p2psession_start). Sent RTP packets are split into datagrams of at most
540 bytes, each led by a little-endian message id, fragment count and fragment index.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import struct
import time
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .errors import CannotConnect, ProtocolError

CONNECT_TIMEOUT = 8.0
FRAME_TIMEOUT = 30.0
MAX_PACKET = 2 * 1024 * 1024
MAX_FRAGMENTS = 4096
SEND_CHUNK = 540  # seedonk_p2psession's default MTU for each sent datagram.
AUDIO_CLOCK = 8000  # The RTP clock of payload type 8, which the app's audio uses.
_MAGIC = b"SDNK"
_PEER = tuple[str, int]


def _setup_packet(kind: int, body: bytes) -> bytes:
    return struct.pack("!4sBBBB8sI", _MAGIC, 1, 0, 0, kind, bytes(8), len(body)) + body


def _candidates(data: bytes) -> list[_PEER]:
    """Camera addresses in the relay's 110-byte reply: four 22-byte entries."""
    if len(data) != 110:
        return []
    peers = []
    for offset in range(20, 108, 22):
        address = data[offset : offset + 4]
        number = int.from_bytes(data[offset + 20 : offset + 22], "big")
        if any(address) and number:
            peers.append((str(ipaddress.IPv4Address(address)), number))
    return peers


def _video_records(payload: bytes, key: bytes) -> list[bytes]:
    """The records of one camera media envelope, decrypted when it is marked encrypted.

    A malformed tail ends the list; the records before it are still returned.
    """
    if len(payload) < 10 or payload[:2] != b"w1":
        return []
    if int.from_bytes(payload[2:6], "little") + 6 != len(payload):
        return []
    body = payload[10:]
    if payload[6] >> 5 == 1:
        if len(key) != 16 or len(body) < 32:
            return []
        try:
            decoder = Cipher(algorithms.AES(key), modes.CBC(body[:16])).decryptor()
            padded = decoder.update(body[16:]) + decoder.finalize()
            unpad = PKCS7(128).unpadder()
            body = unpad.update(padded) + unpad.finalize()
        except ValueError:
            return []
    records = []
    cursor = 0
    while cursor + 4 <= len(body):
        size = int.from_bytes(body[cursor : cursor + 4], "little")
        cursor += 4
        if not size or cursor + size > len(body):
            break
        records.append(body[cursor : cursor + size])
        cursor += size
    return records


class _Port(asyncio.DatagramProtocol):
    def __init__(self, owner: TendRtp, index: int) -> None:
        self.owner = owner
        self.index = index
        self.transport: asyncio.DatagramTransport | None = None
        self.number = 0
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
    """One video receiver: port 0 carries media, port 1 its control channel."""

    def __init__(
        self,
        on_record: Callable[[bytes], None],
        on_end: Callable[[CannotConnect], None] | None = None,
        *,
        kind: bytes = b"V",
        watch_silence: bool = True,
    ) -> None:
        if kind not in (b"V", b"A"):
            raise ProtocolError("Invalid Tend media kind")
        self._on_record = on_record
        self._on_end = on_end
        # "V" for video; "A" for the talkback connection, which receives no video.
        self._kind = kind
        self._watch_silence = watch_silence
        self._ports: list[_Port] = []
        self._relay: _PEER = ("", 0)
        self._camera_ip = ""
        self._source_ip = ""
        # The relay, and the camera addresses the relay announced.
        self._allowed: set[str] = set()
        self._identity = b""
        self._key = b""
        self._started = False
        self._ready = asyncio.Event()
        self._ended = asyncio.Event()
        self._maintenance: asyncio.Task | None = None
        self._group: bytes | None = None
        self._parts: list[bytes] = []
        self._part_bytes = 0
        self._oversize = False
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
        # Counters for diagnostics: camera packets assembled, and those without video.
        self.packets = 0
        self.unreadable = 0
        # Sending (talkback): RTP sequence, the first send's time, the datagram message id.
        self._send_sequence = secrets.randbits(16)
        self._send_started: float | None = None
        self._message_id = 0

    @property
    def path(self) -> str | None:
        """How media reaches this host: the camera's network, Tend's relay, or elsewhere."""
        peer = self._ports[0].peer if self._ports and self._ports[0].ready else None
        if peer is None:
            return None
        if peer[0] == self._relay[0]:
            return "relay"
        return "local" if ipaddress.ip_address(peer[0]).is_private else "remote"

    async def start(self, relay: _PEER, account_alias: str, local_ip: str, aes_key: bytes) -> None:
        """Register the paired UDP ports and complete the camera's connectivity checks.

        ``local_ip`` is the camera's address on its own network, when the broker gave one.
        """
        if self._started or self._ended.is_set():
            raise ProtocolError("A Tend media receiver cannot be reused")
        self._started = True
        try:
            identity = (self._kind.decode("ascii") + "_" + account_alias).encode("ascii")
        except (UnicodeError, TypeError):
            raise ProtocolError("Invalid Tend media identity") from None
        if not account_alias or b"\0" in identity:
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
                self._allowed = {self._relay[0]} | ({local_ip} if local_ip else set())
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
        """Bind consecutive ports; wildcard binding lets the kernel pick each source address."""
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
        """Register with the relay until it names the camera, then probe the camera."""
        sequence = 0
        while not self._ready.is_set():
            if self._ended.is_set():
                raise CannotConnect("Tend video UDP receiver was closed")
            for port in self._ports:
                if port.peer is None:
                    registration = (
                        struct.pack(
                            "!BBBBc4s16sH",
                            1,
                            0,
                            0,
                            port.index,
                            self._kind,
                            socket.inet_aton(self._source_ip),
                            bytes(16),
                            port.number,
                        )
                        + self._identity * 2
                    )
                    port.send(_setup_packet(1, registration), self._relay)
                elif not port.ready:
                    port.send(
                        _setup_packet(3, bytes([1, sequence]) + self._identity * 2), port.peer
                    )
                    port.send(_setup_packet(4, b"\x01"), port.peer)
            sequence = min(2, sequence + 1)
            try:
                async with asyncio.timeout(0.1):
                    await self._ready.wait()
            except TimeoutError:
                pass

    def _receive(self, port: _Port, data: bytes, remote: _PEER) -> None:
        if self._ended.is_set() or remote[0] not in self._allowed:
            return
        if data[:4] == _MAGIC and len(data) >= 20:
            self._setup(port, data, remote)
        elif not port.ready:
            # Media and reports count only once the camera has confirmed this port:
            # nothing can be put into the stream before the handshake.
            return
        elif port.index:
            self._rtcp(data)
        else:
            self._fragment(data)

    def _setup(self, port: _Port, data: bytes, remote: _PEER) -> None:
        kind = data[7]
        if kind == 2:
            # Once the camera has confirmed a port, a late or repeated relay reply must
            # not move it: receiver reports would then go where the camera is not.
            if remote[0] != self._relay[0] or port.ready:
                return
            peers = _candidates(data)
            self._allowed.update(address for address, _ in peers)
            private = [
                peer
                for peer in peers
                if ipaddress.ip_address(peer[0]).is_private and peer[0] != self._source_ip
            ]
            # The camera's own network address first, then any private one, then the
            # last announced (its public address).
            port.peer = (
                next((peer for peer in peers if peer[0] == self._camera_ip), None)
                or (private[0] if private else None)
                or (peers[-1] if peers else None)
            )
            if port.peer is not None:
                port.send(
                    _setup_packet(100, bytes([1, port.index]) + self._kind + self._identity * 2),
                    self._relay,
                )
        elif kind == 3:
            # The camera may answer from another announced address than the one probed.
            port.peer = remote
            for sequence in range(3):
                port.send(_setup_packet(3, bytes([1, sequence]) + self._identity * 2), remote)
        elif kind == 4:
            port.peer = remote
            port.send(_setup_packet(5, data[20:21] or b"\x01"), remote)
        elif kind == 5:
            port.peer = remote
            port.ready = True
            if len(self._ports) == 2 and all(endpoint.ready for endpoint in self._ports):
                self._ready.set()

    def _fragment(self, data: bytes) -> None:
        """Collect one camera packet: its datagrams share the first four header bytes.

        The second four header bytes are not understood, so datagrams are joined in
        arrival order. A lost one costs that packet (its length no longer matches).
        Reordered ones keep the length: an encrypted packet then fails to decrypt, a
        plain one is passed on scrambled and shows as a damaged picture.
        """
        if len(data) < 8:
            return
        group = data[:4]
        if group != self._group:
            self._complete_packet()
            self._group = group
            self._oversize = False
        if self._oversize:
            return
        if len(self._parts) >= MAX_FRAGMENTS or self._part_bytes + len(data) - 8 > MAX_PACKET:
            self._parts.clear()
            self._part_bytes = 0
            self._oversize = True
            return
        self._parts.append(data[8:])
        self._part_bytes += len(data) - 8

    def _complete_packet(self) -> None:
        if not self._parts:
            return
        packet = b"".join(self._parts)
        self._parts.clear()
        self._part_bytes = 0
        if len(packet) < 12 or packet[0] >> 6 != 2:
            return
        self.packets += 1
        self._observe(packet)
        records = _video_records(packet[12:], self._key)
        if not records:
            self.unreadable += 1
            return
        self._last_video_at = time.monotonic()
        for record in records:
            if self._ended.is_set():
                return
            self._on_record(record)

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
            if data[offset + 1] == 200 and size >= 20:
                # The middle 32 bits of the sender report's NTP time.
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
        """Receiver reports keep the camera sending; silence ends the receiver."""
        while not self._ended.is_set():
            await asyncio.sleep(2)
            if self._watch_silence and time.monotonic() - self._last_video_at > FRAME_TIMEOUT:
                self._finish(CannotConnect("Tend camera stopped sending video"))
                return
            report = self._report()
            if report is not None and len(self._ports) == 2:
                port = self._ports[1]
                if port.peer is not None:
                    port.send(report, port.peer)

    def send(self, payload: bytes, payload_type: int = 8) -> None:
        """Send one RTP packet to the camera over the media port, in datagram fragments.

        The timestamp counts the payload clock from the first packet sent; the marker
        bit is clear (oRTP's rtp_session_send_with_ts, as the app sends talkback).
        """
        port = self._ports[0] if self._ports else None
        if self._ended.is_set() or port is None or not port.ready or port.peer is None:
            raise CannotConnect("Tend media connection is not open")
        if not 0 <= payload_type <= 127 or len(payload) > MAX_PACKET:
            raise ProtocolError("Invalid Tend media packet")
        now = time.monotonic()
        if self._send_started is None:
            self._send_started = now
        timestamp = round((now - self._send_started) * AUDIO_CLOCK) & 0xFFFFFFFF
        packet = (
            struct.pack("!BBHII", 0x80, payload_type, self._send_sequence, timestamp, self._ssrc)
            + payload
        )
        self._send_sequence = (self._send_sequence + 1) & 0xFFFF
        count = -(-len(packet) // SEND_CHUNK)
        for index in range(count):
            chunk = packet[index * SEND_CHUNK : (index + 1) * SEND_CHUNK]
            port.send(struct.pack("<IHH", self._message_id, count, index) + chunk, port.peer)
        self._message_id = (self._message_id + 1) & 0xFFFFFFFF

    def _finish(self, error: CannotConnect | None = None) -> None:
        if self._ended.is_set():
            return
        self._ended.set()
        self._parts.clear()
        self._key = b""
        self._identity = b""
        for port in self._ports:
            if port.index and port.peer is not None:
                bye = struct.pack("!BBHI", 0x81, 203, 1, self._ssrc)
                port.send(bye, port.peer)
                port.send(bye, port.peer)
            if port.transport is not None:
                port.transport.close()
        if error is not None and self._on_end is not None:
            self._on_end(error)

    async def close(self) -> None:
        """Close both sockets, including during setup."""
        self._finish()
        if self._maintenance is not None:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
            self._maintenance = None

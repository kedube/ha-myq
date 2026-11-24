"""Read back the transport stream the TC camera source serves, as a player would."""

import asyncio
from dataclasses import dataclass, field

from custom_components.myq.api.mpegts import AUDIO_PID, PACKET_SIZE, PMT_PID, VIDEO_PID, crc32


@dataclass
class Stream:
    """What a transport stream carried: the tables, and each PID's PES packets."""

    streams: dict[int, tuple[int, bytes]] = field(default_factory=dict)  # PID: type, info
    pes: dict[int, list[tuple[int, bytes]]] = field(default_factory=dict)  # PID: pts, data
    random_access: int = 0
    pcr: list[int] = field(default_factory=list)

    @property
    def video(self) -> bytes:
        return b"".join(data for _, data in self.pes.get(VIDEO_PID, []))

    @property
    def opus(self) -> list[tuple[int, bytes]]:
        """Each audio PES's time and Opus packet, with the control header removed."""
        packets = []
        for pts, data in self.pes.get(AUDIO_PID, []):
            assert data[:2] == b"\x7f\xe0"
            size, cursor = 0, 2
            while data[cursor] == 0xFF:
                size, cursor = size + 255, cursor + 1
            size += data[cursor]
            assert len(data) == cursor + 1 + size
            packets.append((pts, data[cursor + 1 :]))
        return packets


def _pts(data: bytes) -> int:
    return (
        ((data[0] >> 1) & 0x07) << 30
        | data[1] << 22
        | (data[2] >> 1) << 15
        | data[3] << 7
        | data[4] >> 1
    )


def parse(data: bytes, counters: dict[int, int] | None = None) -> Stream:
    """Demultiplex whole packets, checking the layout a demuxer relies on."""
    assert len(data) % PACKET_SIZE == 0
    counters = {} if counters is None else counters
    stream = Stream()
    pending: dict[int, bytearray] = {}
    for offset in range(0, len(data), PACKET_SIZE):
        packet = data[offset : offset + PACKET_SIZE]
        assert packet[0] == 0x47
        start = bool(packet[1] & 0x40)
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        control, counter = (packet[3] >> 4) & 3, packet[3] & 0x0F
        if pid in counters:
            assert counter == (counters[pid] + 1) & 0x0F, "continuity"
        counters[pid] = counter
        body = packet[4:]
        if control == 3:
            length = body[0]
            if length:
                flags = body[1]
                if flags & 0x40:
                    stream.random_access += 1
                if flags & 0x10:
                    stream.pcr.append(int.from_bytes(body[2:6], "big") << 1 | body[6] >> 7)
            body = body[1 + length :]
        else:
            assert control == 1
        if pid in (0, PMT_PID):
            assert start and body[0] == 0
            length = ((body[2] & 0x0F) << 8) | body[3]
            section = body[1 : 4 + length]
            assert crc32(section[:-4]) == int.from_bytes(section[-4:], "big")
            if pid == PMT_PID:
                stream.streams = {}
                assert ((section[8] & 0x1F) << 8) | section[9] == VIDEO_PID  # PCR PID
                cursor = 12 + (((section[10] & 0x0F) << 8) | section[11])
                while cursor < len(section) - 4:
                    kind = section[cursor]
                    elementary = ((section[cursor + 1] & 0x1F) << 8) | section[cursor + 2]
                    size = ((section[cursor + 3] & 0x0F) << 8) | section[cursor + 4]
                    info = section[cursor + 5 : cursor + 5 + size]
                    stream.streams[elementary] = (kind, info)
                    cursor += 5 + size
            continue
        if start:
            if pending.get(pid):
                _finish(stream, pid, pending[pid])
            pending[pid] = bytearray(body)
        elif pid in pending:
            pending[pid] += body
        bounded = pending.get(pid)
        if bounded is not None and len(bounded) >= 6:
            size = int.from_bytes(bounded[4:6], "big")
            if size and len(bounded) >= 6 + size:
                assert len(bounded) == 6 + size, "PES fills its packets exactly"
                _finish(stream, pid, pending.pop(pid))
    for pid, rest in pending.items():
        # A PES without a length ends where the data does; one with a length is
        # complete only once all of it has arrived.
        if len(rest) >= 6 and not int.from_bytes(rest[4:6], "big"):
            _finish(stream, pid, rest)
    return stream


def _finish(stream: Stream, pid: int, pes: bytes) -> None:
    assert pes[:3] == b"\x00\x00\x01"
    assert pes[7] & 0x80, "every PES has a time"
    header = 9 + pes[8]
    stream.pes.setdefault(pid, []).append((_pts(pes[9:14]), bytes(pes[header:])))


def video(data: bytes) -> bytes:
    """The H.264 bytes of a stretch of transport stream."""
    return parse(data).video


async def read_video(response, size: int, counters: dict[int, int] | None = None) -> bytes:
    """Read whole packets from a served stream until ``size`` bytes of H.264 arrive.

    A PES without a length (larger than 64 KiB) ends at the next one's start, so it is
    counted as it arrives.
    """
    data = bytearray()
    async with asyncio.timeout(2):
        while len(parse(bytes(data)).video) < size:
            data += await response.content.readexactly(PACKET_SIZE)
    found = parse(bytes(data), counters).video
    assert len(found) == size, (len(found), size)
    return found


async def read_stream(response, packets: int) -> Stream:
    async with asyncio.timeout(2):
        return parse(await response.content.readexactly(packets * PACKET_SIZE))


async def read_until(response, done) -> Stream:
    """Read whole packets until ``done(stream)`` holds for what has arrived."""
    data = bytearray()
    async with asyncio.timeout(2):
        while not done(stream := parse(bytes(data))):
            data += await response.content.readexactly(PACKET_SIZE)
    return stream


__all__ = [
    "AUDIO_PID",
    "VIDEO_PID",
    "Stream",
    "parse",
    "read_stream",
    "read_until",
    "read_video",
    "video",
]

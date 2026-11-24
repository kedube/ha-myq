"""Write H.264 video with a camera's audio as an MPEG transport stream.

Only what Home Assistant's players read is written: one program, a PAT and PMT, video
on one PID with the clock reference, and Opus audio on another (stream type 0x06 with
the "Opus" registration descriptor, each packet behind its control header), which both
go2rtc and FFmpeg read. Nothing is decoded or re-encoded here; timestamps are the
90 kHz times the caller gives.
"""

from __future__ import annotations

PACKET_SIZE = 188
CLOCK = 90000
PAT_PID = 0
PMT_PID = 0x1000
VIDEO_PID = 0x100
AUDIO_PID = 0x101
_PAYLOAD = PACKET_SIZE - 4
_PTS_MASK = (1 << 33) - 1


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def crc32(data: bytes) -> int:
    """The MPEG-2 CRC of a table section."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ byte) & 0xFF]
    return crc


def opus_samples(packet: bytes) -> int | None:
    """The duration of one Opus packet in 48 kHz samples, from its TOC (RFC 6716 3.1)."""
    if not packet:
        return None
    config, code = packet[0] >> 3, packet[0] & 3
    if config < 12:
        size = (480, 960, 1920, 2880)[config & 3]
    elif config < 16:
        size = (480, 960)[config & 1]
    else:
        size = (120, 240, 480, 960)[config & 3]
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    elif len(packet) < 2:
        return None
    else:
        frames = packet[1] & 0x3F
    samples = size * frames
    return samples if 0 < samples <= 5760 else None


def _timestamp(marker: int, value: int) -> bytes:
    value &= _PTS_MASK
    return bytes(
        (
            marker | ((value >> 29) & 0x0E) | 1,
            (value >> 22) & 0xFF,
            ((value >> 14) & 0xFE) | 1,
            (value >> 7) & 0xFF,
            ((value << 1) & 0xFE) | 1,
        )
    )


def _pcr(value: int) -> bytes:
    base = value & _PTS_MASK
    return bytes(
        (
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E,
            0,
        )
    )


def _pes(stream_id: int, pts: int, payload: bytes, *, bounded: bool = True) -> bytes:
    """A PES packet with a presentation time.

    An unbounded one (length 0) runs until the next PES on its PID begins, so more of
    the same access unit can follow it.
    """
    length = 8 + len(payload)
    return (
        b"\x00\x00\x01"
        + bytes((stream_id,))
        + (length if bounded and length <= 0xFFFF else 0).to_bytes(2, "big")
        + b"\x84\x80\x05"  # Data aligned; a PTS only, in five bytes.
        + _timestamp(0x20, pts)
        + payload
    )


class TsMuxer:
    """Packets for one output: continuity counters run per PID."""

    def __init__(self) -> None:
        self._counters: dict[int, int] = {}

    def _counter(self, pid: int) -> int:
        value = self._counters.get(pid, 15)
        value = (value + 1) & 0x0F
        self._counters[pid] = value
        return value

    def _section(self, pid: int, section: bytes) -> bytes:
        body = b"\x00" + section + crc32(section).to_bytes(4, "big")
        header = bytes((0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10 | self._counter(pid)))
        return header + body + b"\xff" * (_PAYLOAD - len(body))

    def tables(self, audio: bool = False, channels: int = 1) -> bytes:
        """The PAT and the PMT: video, and Opus audio when there is some."""
        program = (1).to_bytes(2, "big") + (0xE000 | PMT_PID).to_bytes(2, "big")
        pat = b"\x00" + (0xB000 | (5 + len(program) + 4)).to_bytes(2, "big")
        pat += b"\x00\x01\xc1\x00\x00" + program
        streams = b"\x1b" + (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00"
        if audio:
            # The registration descriptor, then DVB's Opus extension: mono or stereo.
            info = b"\x05\x04Opus\x7f\x02\x80" + bytes((2 if channels == 2 else 1,))
            streams += (
                b"\x06"
                + (0xE000 | AUDIO_PID).to_bytes(2, "big")
                + (0xF000 | len(info)).to_bytes(2, "big")
                + info
            )
        # The version changes with the streams listed.
        version = 0xC1 | ((1 if audio else 0) << 1)
        body = (
            b"\x00\x01"
            + bytes((version,))
            + b"\x00\x00"
            + (0xE000 | VIDEO_PID).to_bytes(2, "big")
            + b"\xf0\x00"
            + streams
        )
        pmt = b"\x02" + (0xB000 | (len(body) + 4)).to_bytes(2, "big") + body
        return self._section(PAT_PID, pat) + self._section(PMT_PID, pmt)

    def _packets(
        self,
        pid: int,
        data: bytes,
        *,
        pcr: int | None = None,
        random_access: bool = False,
        start: bool = True,
    ) -> bytes:
        out = bytearray()
        position = 0
        first = True
        while first or position < len(data):
            clock = _pcr(pcr) if first and pcr is not None else b""
            flags = (0x10 if clock else 0) | (0x40 if first and random_access else 0)
            # The adaptation field's flags and fields, after its length byte.
            adaptation = bytes((flags,)) + clock if flags else b""
            room = _PAYLOAD - (1 + len(adaptation) if adaptation else 0)
            chunk = data[position : position + room]
            position += len(chunk)
            if not adaptation and len(chunk) == _PAYLOAD:
                field, control = b"", 0x10
            elif not adaptation and len(chunk) == _PAYLOAD - 1:
                field, control = b"\x00", 0x30  # One byte to spare: an empty field.
            else:
                # The field takes what the payload leaves: length, flags, then stuffing.
                adaptation = adaptation or b"\x00"
                fill = _PAYLOAD - 1 - len(adaptation) - len(chunk)
                field = bytes((len(adaptation) + fill,)) + adaptation + b"\xff" * fill
                control = 0x30
            begins = 0x40 if first and start else 0
            out += bytes((0x47, begins | (pid >> 8), pid & 0xFF, control | self._counter(pid)))
            out += field + chunk
            first = False
        return bytes(out)

    def video(self, pts: int, data: bytes, keyframe: bool) -> bytes:
        """The start of one H.264 picture (Annex B), with the clock reference 200 ms behind.

        The PES is unbounded, as FFmpeg writes video: readers take the picture as
        complete when the next one begins, so further slices can still join it.
        """
        return self._packets(
            VIDEO_PID,
            _pes(0xE0, pts, data, bounded=False),
            pcr=pts - CLOCK // 5,
            random_access=keyframe,
        )

    def video_continued(self, data: bytes) -> bytes:
        """More of the picture the last ``video`` began: its further slices."""
        return self._packets(VIDEO_PID, data, start=False)

    def audio(self, pts: int, packet: bytes) -> bytes:
        """One Opus packet, behind its control header (private stream 1)."""
        size = len(packet)
        header = b"\x7f\xe0" + b"\xff" * (size // 255) + bytes((size % 255,))
        return self._packets(AUDIO_PID, _pes(0xBD, pts, header + packet))

"""A TC camera's microphone as Opus packets on the live stream's 90 kHz timeline.

The camera sends Opus or G.711 µ-law (Tend's codec numbers 4 and 2). Opus is passed on
as the camera encoded it. µ-law is re-encoded as Opus, because the transport stream's
players read no G.711 from it, and FFmpeg fails on the stream type go2rtc reads A-law
under. Each packet's time follows from the one before it, so the audio plays without
gaps, and is moved back to the arrival time when the two drift apart.
"""

from __future__ import annotations

import av

from .mpegts import CLOCK, opus_samples

G711U = 2
OPUS = 4
CODEC_NAMES = {G711U: "g711", OPUS: "opus"}
MAX_DRIFT = CLOCK // 4  # 250 ms between a packet's place on the timeline and its arrival.
MAX_GAP = 50  # RTP sequence steps read as lost packets; a larger jump is a restart.
_RATE = 8000
_FRAME = _RATE // 50  # 20 ms of µ-law


def _ulaw_to_linear(value: int) -> int:
    value = ~value & 0xFF
    sample = (((value & 0x0F) << 3) + 0x84) << ((value & 0x70) >> 4)
    return (0x84 - sample) if value & 0x80 else (sample - 0x84)


_LINEAR = [_ulaw_to_linear(value).to_bytes(2, "little", signed=True) for value in range(256)]


def ulaw_to_pcm(data: bytes) -> bytes:
    """G.711 µ-law samples as signed 16-bit little-endian PCM."""
    return b"".join(_LINEAR[value] for value in data)


class LiveAudio:
    """One camera session's audio: the first codec heard is the one carried."""

    def __init__(self) -> None:
        self.codec: int | None = None
        self.channels = 1
        self.frames = 0  # Camera frames carried.
        self.dropped = 0  # Frames in another codec, unreadable, duplicated or late.
        self._encoder: av.AudioCodecContext | None = None
        self._encoder_failed = False
        self._pcm = bytearray()
        self._samples = 0
        self._next: int | None = None
        self._sequence: int | None = None

    def _encode(self, ulaw: bytes) -> list[bytes] | None:
        """µ-law as 20 ms Opus packets at 8 kHz; None when Opus cannot be encoded here."""
        if self._encoder is None:
            if self._encoder_failed:
                return None
            try:
                context = av.CodecContext.create("libopus", "w")
                if not isinstance(context, av.AudioCodecContext):
                    raise ValueError("not an audio encoder")
                context.options = {"application": "voip", "frame_duration": "20"}
                context.sample_rate, context.layout, context.format = _RATE, "mono", "s16"
                context.bit_rate = 16000
                context.open()
            except (av.FFmpegError, ValueError):
                self._encoder_failed = True
                return None
            self._encoder = context
        self._pcm += ulaw_to_pcm(ulaw)
        packets: list[bytes] = []
        try:
            while len(self._pcm) >= _FRAME * 2:
                frame = av.AudioFrame(format="s16", layout="mono", samples=_FRAME)
                frame.planes[0].update(bytes(self._pcm[: _FRAME * 2]))
                del self._pcm[: _FRAME * 2]
                frame.sample_rate, frame.pts = _RATE, self._samples
                self._samples += _FRAME
                packets.extend(bytes(packet) for packet in self._encoder.encode(frame))
        except (av.FFmpegError, ValueError):
            self._encoder, self._encoder_failed = None, True
            return None
        return packets

    def packets(
        self, codec: int, data: bytes, sequence: int | None, now: int
    ) -> list[tuple[int, bytes]]:
        """The Opus packets one camera frame yields, each with its 90 kHz time.

        ``sequence`` is the frame's RTP sequence number (None over the TLS channel);
        ``now`` is the stream's clock as the frame arrived.
        """
        if self.codec is None:
            if codec not in CODEC_NAMES or (codec == OPUS and opus_samples(data) is None):
                self.dropped += 1
                return []
            self.codec = codec
            if codec == OPUS:
                self.channels = 2 if data[0] & 0x04 else 1
        if codec != self.codec or not data:
            self.dropped += 1
            return []
        missing = 0
        if sequence is not None:
            if self._sequence is not None:
                step = (sequence - self._sequence) & 0xFFFF
                if step == 0 or step >= 0x8000:
                    self.dropped += 1  # Repeated, or behind one already placed.
                    return []
                missing = step - 1 if step <= MAX_GAP else 0
            self._sequence = sequence
        if codec == OPUS:
            samples = opus_samples(data)
            if samples is None:
                self.dropped += 1
                return []
            duration = samples * CLOCK // 48000
            out = [(data, duration)]
        else:
            encoded = self._encode(data)
            if encoded is None:
                self.dropped += 1
                return []
            duration = len(data) * CLOCK // _RATE
            out = [(packet, CLOCK // 50) for packet in encoded]
        self.frames += 1
        if self._next is not None:
            self._next += missing * duration  # Lost frames keep their place.
        placed = []
        for packet, length in out:
            if self._next is None or abs(now - self._next) > MAX_DRIFT:
                self._next = now
            placed.append((self._next, packet))
            self._next += length
        return placed

"""The live stream's transport stream writer and the camera audio it carries."""

import io
import math
from types import SimpleNamespace

import av
import pytest

from custom_components.myq.api import live_audio
from custom_components.myq.api.live_audio import (
    G711U,
    MAX_DRIFT,
    OPUS,
    LiveAudio,
    ulaw_to_pcm,
)
from custom_components.myq.api.mpegts import (
    AUDIO_PID,
    CLOCK,
    PACKET_SIZE,
    VIDEO_PID,
    TsMuxer,
    crc32,
    opus_samples,
)

from .ts import parse

FRAME = 1800  # 20 ms at 90 kHz.


def opus_packets(count=10, rate=16000):
    """Real 20 ms Opus packets of a tone, as a camera's encoder would send."""
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate, encoder.layout, encoder.format = rate, "mono", "s16"
    samples = rate // 50
    packets = []
    for index in range(count):
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        tone = b"".join(
            int(8000 * math.sin((index * samples + n) / 5)).to_bytes(2, "little", signed=True)
            for n in range(samples)
        )
        frame.planes[0].update(tone)
        frame.sample_rate, frame.pts = rate, index * samples
        packets.extend(bytes(packet) for packet in encoder.encode(frame))
    return packets


def ulaw(sample: int) -> int:
    """G.711 µ-law of one linear sample (the inverse of ulaw_to_pcm, for test input)."""
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), 32635) + 0x84
    exponent = max(0, min(7, sample.bit_length() - 8))
    return ~(sign | (exponent << 4) | ((sample >> (exponent + 3)) & 0x0F)) & 0xFF


def test_crc_is_the_mpeg2_check():
    assert crc32(b"123456789") == 0x0376E6E7


@pytest.mark.parametrize("size", [0, 1, 160, 161, 162, 163, 164, 165, 166, 175, 176, 177, 400])
def test_packets_are_filled_exactly_whatever_the_payload_size(size):
    """Stuffing goes in the adaptation field: one spare byte is an empty field."""
    muxer = TsMuxer()
    data = muxer.video(CLOCK, bytes([0x41]) * size, False) + muxer.video_continued(b"\x99" * size)
    data += muxer.audio(CLOCK, b"\xfc" * size or b"\xfc")
    stream = parse(muxer.tables(True) + data)
    assert stream.pes[VIDEO_PID] == [(CLOCK, bytes([0x41]) * size + b"\x99" * size)]
    assert stream.opus == [(CLOCK, b"\xfc" * size or b"\xfc")]
    assert stream.pcr == [CLOCK - CLOCK // 5]


def test_tables_list_video_and_the_opus_audio():
    muxer = TsMuxer()
    assert parse(muxer.tables()).streams == {VIDEO_PID: (0x1B, b"")}
    # The registration descriptor first (as go2rtc reads it), then DVB's channel count.
    assert parse(muxer.tables(True)).streams == {
        VIDEO_PID: (0x1B, b""),
        AUDIO_PID: (0x06, b"\x05\x04Opus\x7f\x02\x80\x01"),
    }
    assert parse(muxer.tables(True, 2)).streams[AUDIO_PID][1][-1] == 2


def test_long_opus_packets_and_large_pictures():
    muxer = TsMuxer()
    picture = b"\x00\x00\x00\x01\x65" + b"\x33" * 70000  # Too large for a PES length.
    packet = b"\xfc" + b"\x01" * 600  # Its size is written as 255 + 255 + 91.
    stream = parse(muxer.tables(True) + muxer.video(CLOCK, picture, True) + muxer.audio(5, packet))
    assert stream.video == picture and stream.random_access == 1
    assert stream.opus == [(5, packet)]


def test_ffmpeg_reads_the_stream_as_h264_and_opus():
    """Home Assistant's stream worker opens the source with FFmpeg."""
    muxer = TsMuxer()
    packets = opus_packets()
    data = bytearray(muxer.tables(True))
    data += muxer.video(CLOCK, b"\x00\x00\x00\x01\x65" + b"\x33" * 2000, True)
    for index, packet in enumerate(packets):
        data += muxer.audio(CLOCK + index * FRAME, packet)
    data += muxer.video(CLOCK + 9000, b"\x00\x00\x00\x01\x41" + b"\x44" * 200, False)
    with av.open(io.BytesIO(bytes(data)), format="mpegts") as container:
        assert [(s.type, s.codec_context.name) for s in container.streams] == [
            ("video", "h264"),
            ("audio", "opus"),
        ]
        assert container.streams.audio[0].codec_context.channels == 1
        audio = [
            (packet.pts, bytes(packet))
            for packet in container.demux(container.streams.audio[0])
            if packet.size
        ]
    assert audio == [(CLOCK + index * FRAME, packet) for index, packet in enumerate(packets)]


@pytest.mark.parametrize(
    "packet,samples",
    [
        (b"\x78", 960),  # CELT, 20 ms, one frame.
        (b"\x79", 1920),  # Two frames.
        (b"\x7b\x03", 2880),  # Three, counted in the second byte.
        (b"\x00", 480),  # SILK narrowband, 10 ms.
        (b"\x08", 960),  # And 20 ms.
        (b"\x60", 480),  # Hybrid, 10 ms.
        (b"\x7b", None),  # The count is missing.
        (b"\x7b\x00", None),
        (b"\x1b\x3f", None),  # Longer than 120 ms.
        (b"", None),
    ],
)
def test_opus_duration_comes_from_its_table_of_contents(packet, samples):
    assert opus_samples(packet) == samples


def test_ulaw_decodes_to_linear_pcm():
    assert ulaw_to_pcm(bytes([0xFF, 0x7F, 0x00, 0x80])) == b"".join(
        value.to_bytes(2, "little", signed=True) for value in (0, 0, -32124, 32124)
    )
    for sample in (-30000, -1000, -5, 0, 5, 1000, 30000):
        decoded = int.from_bytes(ulaw_to_pcm(bytes([ulaw(sample)])), "little", signed=True)
        assert abs(decoded - sample) <= max(8, abs(sample) // 16)


def test_opus_is_carried_as_sent_on_a_continuous_timeline():
    audio = LiveAudio()
    packets = opus_packets(5)
    # Arrival jitter does not move the timeline.
    arrivals = [CLOCK, CLOCK + 3000, CLOCK + 3100, CLOCK + 5000, CLOCK + 7300]
    placed = [
        audio.packets(OPUS, packet, sequence, now)
        for sequence, (packet, now) in enumerate(zip(packets, arrivals, strict=True), 100)
    ]
    assert placed == [[(CLOCK + index * FRAME, packet)] for index, packet in enumerate(packets)]
    assert (audio.codec, audio.channels, audio.frames, audio.dropped) == (OPUS, 1, 5, 0)


def test_lost_frames_keep_their_place_and_late_ones_are_dropped():
    audio = LiveAudio()
    packet = opus_packets(1)[0]
    assert audio.packets(OPUS, packet, 65534, CLOCK) == [(CLOCK, packet)]
    # Two lost across the sequence wrap: this one is three frames on.
    assert audio.packets(OPUS, packet, 1, CLOCK + 3 * FRAME) == [(CLOCK + 3 * FRAME, packet)]
    assert audio.packets(OPUS, packet, 1, CLOCK + 4 * FRAME) == []  # Repeated.
    assert audio.packets(OPUS, packet, 0, CLOCK + 4 * FRAME) == []  # Behind.
    assert audio.dropped == 2
    # Over the TLS channel there is no sequence: each frame follows the last.
    assert audio.packets(OPUS, packet, None, CLOCK + 4 * FRAME) == [(CLOCK + 4 * FRAME, packet)]


def test_timeline_returns_to_arrival_time_after_a_pause():
    audio = LiveAudio()
    packet = opus_packets(1)[0]
    audio.packets(OPUS, packet, None, CLOCK)
    late = CLOCK + FRAME + MAX_DRIFT + 1
    assert audio.packets(OPUS, packet, None, late) == [(late, packet)]


def test_only_the_first_codec_heard_is_carried():
    audio = LiveAudio()
    assert audio.packets(1, b"\x00" * 640, None, CLOCK) == []  # LPCM: not carried.
    assert audio.packets(OPUS, b"", None, CLOCK) == []  # Not Opus.
    packet = opus_packets(1)[0]
    assert audio.packets(OPUS, packet, None, CLOCK)
    assert audio.packets(G711U, b"\xff" * 160, None, CLOCK) == []
    assert audio.dropped == 3 and audio.codec == OPUS


def test_ulaw_is_reencoded_as_opus_in_20_ms_packets():
    audio = LiveAudio()
    tone = bytes(ulaw(int(8000 * math.sin(2 * math.pi * 440 * n / 8000))) for n in range(320))
    placed = audio.packets(G711U, tone, 7, CLOCK)
    placed += audio.packets(G711U, tone[:80], 8, CLOCK + 2 * FRAME)  # Half a frame: held.
    placed += audio.packets(G711U, tone[80:160], 9, CLOCK + 2 * FRAME + 900)
    assert [pts for pts, _ in placed] == [CLOCK, CLOCK + FRAME, CLOCK + 2 * FRAME]
    assert all(opus_samples(packet) == 960 for _, packet in placed)
    decoder = av.CodecContext.create("opus", "r")
    decoded = sum(
        frame.samples
        for _, packet in placed
        for frame in decoder.decode(av.Packet(packet))  # type: ignore[attr-defined]
    )
    # Opus decodes at 48 kHz; the decoder holds back its start-up delay.
    assert 2 * 960 < decoded <= 3 * 960
    assert audio.codec == G711U and audio.frames == 3


def test_ulaw_without_an_opus_encoder_is_dropped(monkeypatch):
    def missing(*args, **kwargs):
        raise av.FFmpegError(0, "no libopus")

    fake = SimpleNamespace(
        CodecContext=SimpleNamespace(create=missing),
        FFmpegError=av.FFmpegError,
        AudioCodecContext=av.AudioCodecContext,
        AudioFrame=av.AudioFrame,
    )
    monkeypatch.setattr(live_audio, "av", fake)
    audio = LiveAudio()
    assert audio.packets(G711U, b"\xff" * 160, 1, CLOCK) == []
    assert audio.packets(G711U, b"\xff" * 160, 2, CLOCK + FRAME) == []
    assert audio.dropped == 2


def test_every_packet_is_whole():
    muxer = TsMuxer()
    for size in range(150, 200):
        assert len(muxer.audio(CLOCK, b"\xfc" * size)) % PACKET_SIZE == 0

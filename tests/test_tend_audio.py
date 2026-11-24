"""Talkback messages, encoding and the talk sequence, against synthetic sessions."""

import asyncio
import io
import math
import struct
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.myq.api import tend_legacy
from custom_components.myq.api.errors import CannotConnect, CommandRejected
from custom_components.myq.api.tend_audio import (
    Talkback,
    can_talk,
    data_message,
    encode_audio,
    play_track,
    start_message,
    stop_message,
    stored_tracks,
    talkback_codec,
)
from custom_components.myq.api.tend_channel import JOIN_AUDIO
from custom_components.myq.api.tend_legacy import AudioLink

DEST, SID = b"owner:camera", b"1700000000000"


def wav(seconds=1.0, rate=44100, channels=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = int(seconds * rate)
        sample = [int(8000 * math.sin(index / 20)) for index in range(frames)]
        out.writeframes(b"".join(struct.pack("<h", value) * channels for value in sample))
    return buffer.getvalue()


def test_messages_follow_the_apps_byte_layout():
    common = b"SDNK\x01%c\x01\x00\x00" + struct.pack("<H", len(DEST)) + DEST
    start = start_message(DEST, SID, "opus")
    assert start == (
        (common % 7)
        + struct.pack("<I", len(SID) + 9)
        + bytes([len(SID)])
        + SID
        + b"\x10"
        + bytes(7)
    )
    assert start_message(DEST, SID, "g711")[-8] == 1
    audio = b"\xaa" * 47
    assert data_message(DEST, SID, 5, audio) == (
        (common % 9)
        + struct.pack("<I", len(SID) + 13 + len(audio))
        + bytes([len(SID)])
        + SID
        + struct.pack("<II", 5, len(audio))
        + audio
        + bytes(4)
    )
    assert stop_message(DEST, SID) == (
        (common % 8) + struct.pack("<I", len(SID) + 5) + bytes([len(SID)]) + SID + bytes(4)
    )


@pytest.mark.parametrize(
    "speaker,codec,talks",
    [
        ({"talkback": True, "codecList": ["G711", "OPUS"]}, "opus", True),
        ({"talkback": True, "codecList": ["g711u"]}, "g711", True),
        # Without a usable list the codec comes from the login row once connected.
        ({"talkback": True, "codecList": ["speex"]}, None, True),
        ({"talkback": True}, None, True),
        ({"talkback": "yes", "codecList": ["opus"]}, "opus", False),
        ({"talkback": True, "codecList": "opus"}, None, True),
    ],
)
def test_talkback_needs_the_capability_and_a_codec(speaker, codec, talks):
    details = {"cap": {"spk": speaker}}
    assert talkback_codec(details) == codec
    assert can_talk(details) is talks


def test_stored_tracks_need_playback():
    tracks = [
        {"id": "t1", "descr": "Lullaby", "type": "MUSIC"},
        {"id": "t2"},
        {"id": 3, "descr": "bad"},
        "x",
        {"id": "", "descr": "empty"},
    ]
    assert stored_tracks({"cap": {"spk": {"playback": True, "playbackTrackList": tracks}}}) == [
        ("t1", "Lullaby"),
        ("t2", "t2"),
    ]
    assert stored_tracks({"cap": {"spk": {"playback": False, "playbackTrackList": tracks}}}) == []
    assert stored_tracks({}) == []


async def test_stored_track_is_played_by_id(camera_device):
    client = AsyncMock()
    await play_track(client, camera_device, "t1")
    call = client.device_mutation.await_args
    assert call.args == (
        camera_device,
        "POST",
        "https://media.hubs.tend-us.tendplatform.com",
        f"/cxs/api/devices/{camera_device.camera_id}/audio/player/do/play",
    )
    assert call.kwargs == {"headers": {"AntiCSRF": "AntiCSRF"}, "json": {"trackId": "t1"}}


def test_audio_is_encoded_in_20_ms_frames():
    data = wav(1.0)
    opus = encode_audio(data, "opus")
    assert 50 <= len(opus) <= 52 and len({len(frame) for frame in opus}) == 1
    g711 = encode_audio(data, "g711")
    assert len(g711) == 50 and {len(frame) for frame in g711} == {160}
    assert len(encode_audio(data, "g711", max_seconds=0.5)) == 25
    # A partial last frame is padded to a whole one.
    assert len(encode_audio(wav(0.031), "g711")) == 2


@pytest.mark.parametrize(
    "data",
    [
        b"not audio at all",
        # A playlist is never followed: FFmpeg may not open other inputs.
        b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n",
        wav(0),
    ],
)
def test_unreadable_media_is_refused(data):
    with pytest.raises(CommandRejected):
        encode_audio(data, "opus")
    with pytest.raises(CommandRejected, match="codec"):
        encode_audio(wav(0.1), "speex")


def fake_session(*, key=b"k" * 16, channel=None):
    session = MagicMock()
    session.account_alias, session.camera_address = "owner", "192.168.1.20"
    session._key, session._device_alias = key, "owner:camera"
    session.audio_channel = channel
    session.channel_identity = (b"S" * 16, 42)
    session.media_sources = {"owner:camera", "owner"}
    session.request_audio = AsyncMock(return_value=("relay.seedonk.com", 6000))
    session.audio_command = AsyncMock()

    async def talk_link():
        # No live view: the talk opens the camera's audio connection itself.
        link = AudioLink(session)
        await link.open()
        return link, True

    session.talk_link = talk_link
    return session


@pytest.fixture
def transports(monkeypatch):
    made = {"rtp": [], "channel": []}

    def rtp(on_record, on_end=None, *, kind=b"V", watch_silence=True, on_audio=None):
        media = MagicMock(start=AsyncMock(), close=AsyncMock(), send=MagicMock(), ended=False)
        media.kind, media.watch_silence, media.on_audio = kind, watch_silence, on_audio
        made["rtp"].append(media)
        return media

    def channel(on_record, on_end=None, *, on_audio=None):
        media = MagicMock(start=AsyncMock(), close=AsyncMock(), write=AsyncMock(), ended=False)
        media.on_audio = on_audio
        made["channel"].append(media)
        return media

    monkeypatch.setattr(tend_legacy, "TendRtp", rtp)
    monkeypatch.setattr(tend_legacy, "TendChannel", channel)
    return made


async def no_wait(_seconds):
    await asyncio.sleep(0)


async def test_talk_over_udp_sends_start_data_and_stop(transports):
    session = fake_session()
    talk = Talkback(session)
    await talk.open()
    session_id = session.request_audio.await_args.args[0]
    assert session_id.startswith("owner:") and session.request_audio.await_args.args[1] == 9
    rtp = transports["rtp"][0]
    assert (rtp.kind, rtp.watch_silence) == (b"A", False)
    # The key decrypts what the camera sends on the connection: its microphone.
    assert rtp.start.await_args.args == (
        ("relay.seedonk.com", 6000),
        "owner",
        "192.168.1.20",
        b"k" * 16,
    )
    session.audio_command.assert_awaited_once_with("start", session_id, 9)
    await talk.stream([b"one", b"two"], "opus", no_wait)
    sid = talk._sid
    assert sid.isdigit()
    assert [call.args[0] for call in rtp.send.call_args_list] == [
        start_message(DEST, sid, "opus"),
        data_message(DEST, sid, 0, b"one"),
        data_message(DEST, sid, 1, b"two"),
        stop_message(DEST, sid),
    ]
    await talk.close()
    await talk.close()
    assert session.audio_command.await_args_list[-1].args == ("stop", session_id, 9)
    assert session.audio_command.await_count == 2
    rtp.close.assert_awaited_once()


async def test_talk_over_a_live_views_connection_leaves_it_open(transports):
    session = fake_session()
    shared = AudioLink(session)
    await shared.open()
    session.talk_link = AsyncMock(return_value=(shared, False))
    talk = Talkback(session)
    await talk.open()
    await talk.stream([b"one"], "g711", no_wait)
    await talk.close()
    rtp = transports["rtp"][0]
    assert [call.args[0][5] for call in rtp.send.call_args_list] == [7, 9, 8]
    # Only the start of the shared connection was sent; the live view still owns it.
    assert [call.args[0] for call in session.audio_command.await_args_list] == ["start"]
    rtp.close.assert_not_awaited()
    assert shared.is_open


async def test_camera_without_a_key_is_asked_for_mode_8(transports):
    session = fake_session(key=b"")
    talk = Talkback(session)
    await talk.open()
    assert session.request_audio.await_args.args[1] == 8
    await talk.close()


async def test_unanswered_request_moves_on_to_the_audio_channel(transports):
    session = fake_session(channel=("a.seedonk.com", 5201))
    session.request_audio.side_effect = [TimeoutError, None]
    talk = Talkback(session)
    await talk.open()
    session_id = session.request_audio.await_args.args[0]
    assert [call.args for call in session.audio_command.await_args_list] == [
        ("stop", session_id, 9),
        ("start", session_id, 0),
    ]
    channel = transports["channel"][0]
    assert channel.start.await_args.args == (
        ("a.seedonk.com", 5201),
        b"S" * 16,
        42,
        {"owner:camera", "owner"},
    )
    assert channel.start.await_args.kwargs == {"join": JOIN_AUDIO}
    await talk.stream([b"x"], "opus", no_wait)
    # Over the channel, messages are written raw.
    assert channel.write.await_args_list[0].args[0][:4] == b"SDNK"
    assert len(channel.write.await_args_list) == 3
    await talk.close()
    channel.close.assert_awaited_once()


async def test_failed_rendezvous_is_stopped_and_reported(transports, monkeypatch):
    session = fake_session()

    def failing(on_record, on_end=None, *, kind=b"V", watch_silence=True, on_audio=None):
        media = MagicMock(start=AsyncMock(side_effect=CannotConnect("UDP blocked")))
        media.close = AsyncMock()
        transports["rtp"].append(media)
        return media

    monkeypatch.setattr(tend_legacy, "TendRtp", failing)
    talk = Talkback(session)
    with pytest.raises(CannotConnect, match="UDP blocked"):
        await talk.open()
    assert session.audio_command.await_args_list[-1].args[0] == "stop"
    transports["rtp"][0].close.assert_awaited_once()


async def test_unanswered_audio_request_without_a_channel_fails(transports):
    session = fake_session()
    session.request_audio.side_effect = TimeoutError
    with pytest.raises(CannotConnect, match="did not answer the audio request"):
        await AudioLink(session).open()


async def test_cancelled_talk_still_sends_stop(transports):
    session = fake_session()
    talk = Talkback(session)
    await talk.open()
    task = asyncio.create_task(talk.stream([b"x"] * 100, "opus"))  # Real 20 ms pacing.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sent = [call.args[0] for call in transports["rtp"][0].send.call_args_list]
    assert sent[0][5] == 7 and sent[-1][5] == 8 and 2 <= len(sent) < 100
    await talk.close()


@pytest.mark.parametrize("over_channel", [False, True])
async def test_camera_audio_on_the_connection_reaches_the_listener(transports, over_channel):
    session = fake_session(channel=("a.seedonk.com", 5201))
    if over_channel:
        session.request_audio.side_effect = [TimeoutError, None]
    heard = []
    link = AudioLink(session, lambda *frame: heard.append(frame))
    await link.open()
    media = transports["channel" if over_channel else "rtp"][0]
    media.on_audio(4, b"opus frame", None if over_channel else 7)
    assert heard == [(4, b"opus frame", None if over_channel else 7)]
    assert link.frames == 1 and link.mode == (0 if over_channel else 9)
    await link.close()
    assert not link.is_open


def test_speaker_summary_reports_capabilities_without_identifiers():
    from custom_components.myq.api.tend_audio import speaker_summary

    details = {
        "cap": {
            "spk": {
                "talkback": True,
                "fullDuplex": False,
                "codecList": [" OPUS ", "g711U", "bad codec name", 5],
                "playback": True,
                "playbackTrackList": [{"id": "t1", "descr": "Rain"}, {"id": 3}],
            }
        }
    }
    assert speaker_summary(details) == {
        "talkback": True,
        "full_duplex": False,
        "codecs": ["g711u", "opus"],
        "talkback_codec": "opus",
        "playback": True,
        "stored_sounds": 1,
    }
    assert speaker_summary({"cap": {"spk": {"talkback": "yes", "codecList": "opus"}}}) == {
        "talkback": None,
        "full_duplex": None,
        "codecs": None,
        "talkback_codec": None,
        "playback": None,
        "stored_sounds": 0,
    }


async def test_talk_learns_the_codec_from_the_cameras_own_sound(transports):
    """Without a listed codec the app talks in the codec it hears (talkbackCodec)."""
    session = fake_session()
    talk = Talkback(session)
    assert await talk.heard_codec() is None  # Not open.
    await talk.open()
    assert await talk.heard_codec(0.01) is None  # Nothing heard yet.
    transports["rtp"][0].on_audio(2, b"\xff" * 160, 1)
    assert await talk.heard_codec() == "g711"
    await talk.close()

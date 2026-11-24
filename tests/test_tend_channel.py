"""Tend's TLS video channel (mode 0) against synthetic broker frames."""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.myq.api import tend_channel
from custom_components.myq.api.errors import CannotConnect, ProtocolError
from custom_components.myq.api.tend import HOST, Frame
from custom_components.myq.api.tend_channel import (
    JOIN_AUDIO,
    TendChannel,
    channel_address,
    channel_audio,
    channel_records,
)

SENDER = b"S" * 16


async def until(condition):
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0)


def media(records, source=b"owner:camera", command=5):
    body = b"".join(struct.pack("<I", len(record)) + record for record in records)
    data = bytes([0x01, 0x10, 0x00, 0x01]) + body  # codec, size index, channel, key frame
    return (
        bytes([command, 0])
        + struct.pack(">I", len(source))
        + source
        + struct.pack(">I", len(data))
        + data
    )


@pytest.mark.parametrize(
    "login,expected",
    [
        (
            {
                "video-channel-host": "VC1.broker.tend-us.tendplatform.com",
                "video-channel-port-aes": "5100",
                "video-channel-port-tls": "5101",
            },
            ("vc1.broker.tend-us.tendplatform.com", 5101),
        ),
        # The older form: lines "socket,<host>,<port>,<TLS port>".
        (
            {"video-host": "http,x.seedonk.com,80\nsocket,v2.seedonk.com,5100,5443,,"},
            ("v2.seedonk.com", 5443),
        ),
        ({"video-hostname": "socket,v3.seedonk.com,5100"}, ("v3.seedonk.com", 5101)),
        ({"video-channel-host": "8.8.8.8", "video-channel-port-tls": "5101"}, ("8.8.8.8", 5101)),
        # Hosts outside Tend's domains, private addresses and bad ports are never used.
        ({"video-channel-host": "evil.example", "video-channel-port-tls": "5101"}, None),
        ({"video-channel-host": "10.0.0.2", "video-channel-port-tls": "5101"}, None),
        ({"video-channel-host": "v.seedonk.com", "video-channel-port-tls": "99999"}, None),
        ({"video-channel-host": "v.seedonk.com", "video-channel-port-tls": "²"}, None),
        ({}, None),
    ],
)
def test_channel_address_comes_from_the_login(login, expected):
    assert channel_address(login, "video") == expected


def test_audio_channel_uses_its_own_keys():
    login = {"audio-channel-host": "a.seedonk.com", "audio-channel-port-tls": "5201"}
    assert channel_address(login, "audio") == ("a.seedonk.com", 5201)
    assert channel_address(login, "video") is None


def test_channel_records_follow_the_server_relay_layout():
    sources = frozenset({"owner:camera"})
    first, second = b"\x00\x00\x00\x01\x67sps", b"\x00\x00\x00\x01\x65idr"
    assert channel_records(media([first, second]), sources) == [first, second]
    # The source is compared without regard to case, as the app does.
    assert channel_records(media([first], b"OWNER:Camera"), sources) == [first]
    assert channel_records(media([first], b"owner:other"), sources) is None
    assert channel_records(media([first], command=6), sources) is None
    assert channel_records(media([first])[:-1], sources) is None  # Length mismatch.
    truncated = media([first]) + b"\x00"
    assert channel_records(truncated, sources) is None
    # A malformed record ends the list; those before it are kept.
    data = bytes([1, 0x10, 0, 1]) + struct.pack("<I", 3) + b"abc" + struct.pack("<I", 99)
    raw = bytes([5, 0]) + struct.pack(">I", 12) + b"owner:camera" + struct.pack(">I", len(data))
    assert channel_records(raw + data, sources) == [b"abc"]


@pytest.fixture
def channel(monkeypatch):
    records, ended = [], []
    receiver = TendChannel(records.append, ended.append)
    reader = asyncio.StreamReader()
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
    opened = []

    async def open_channel(host, port):
        opened.append((host, port))
        receiver._reader, receiver._writer = reader, writer

    monkeypatch.setattr(receiver, "_open", open_channel)
    return receiver, reader, writer, records, ended, opened


async def test_channel_joins_with_the_login_identity_and_relays_records(channel):
    receiver, reader, writer, records, ended, opened = channel
    reader.feed_data(Frame(257).encode())  # Any answer but action 1 accepts the join.
    await receiver.start(("vc.seedonk.com", 5101), SENDER, 42, {"owner:camera", "owner"})
    join = writer.write.call_args_list[0].args[0]
    assert opened == [("vc.seedonk.com", 5101)]
    # Action 508 with service 122, the login's sender and recipient, nothing else.
    assert struct.unpack_from("<H", join, 11)[0] == 122
    assert struct.unpack_from("<H", join, 13)[0] == 508
    assert join[19:27] == struct.pack("<Q", 42) and join[27:43] == SENDER
    assert len(join) == 43
    assert receiver.path == "tcp_relay"
    frame = b"\x00\x00\x00\x01\x65idr"
    reader.feed_data(Frame(257).encode() + Frame(413, data=media([frame])).encode())
    reader.feed_data(Frame(413, data=media([frame], b"intruder")).encode())
    await until(lambda: receiver.unreadable >= 1)
    assert records == [frame] and receiver.packets == 2
    await receiver.close()
    logout = writer.write.call_args_list[-1].args[0]
    assert struct.unpack_from("<H", logout, 13)[0] == 408
    writer.close.assert_called_once()
    assert receiver.path is None and not ended
    with pytest.raises(ProtocolError, match="cannot be reused"):
        await receiver.start(("vc.seedonk.com", 5101), SENDER, 42, {"owner"})


async def test_refused_join_fails_and_closes(channel):
    receiver, reader, writer, *_ = channel
    reader.feed_data(Frame(1).encode())
    with pytest.raises(CannotConnect, match="refused the video channel"):
        await receiver.start(("vc.seedonk.com", 5101), SENDER, 0, {"owner"})
    writer.close.assert_called_once()


async def test_silent_channel_ends_with_an_error(channel, monkeypatch):
    receiver, reader, _writer, _records, ended, _opened = channel
    monkeypatch.setattr(tend_channel, "FRAME_TIMEOUT", 0)
    sleep = asyncio.sleep
    monkeypatch.setattr(tend_channel.asyncio, "sleep", lambda _delay: sleep(0))
    reader.feed_data(Frame(257).encode())
    await receiver.start(("vc.seedonk.com", 5101), SENDER, 0, {"owner"})
    await until(lambda: ended)
    assert "stopped sending video" in str(ended[0])
    await receiver.close()


async def test_channel_accepts_the_front_door_certificate_only_on_a_name_mismatch(monkeypatch):
    names = []

    async def open_connection(host, port, *, ssl, server_hostname):
        names.append(server_hostname)
        if len(names) == 1:
            error = tend_channel.ssl.SSLCertVerificationError("mismatch")
            error.verify_code = 62
            raise error
        return asyncio.StreamReader(), MagicMock()

    monkeypatch.setattr(tend_channel.asyncio, "open_connection", open_connection)
    receiver = TendChannel(lambda record: None)
    await receiver._open("vc.broker.tend-us.tendplatform.com", 5101)
    assert names == ["vc.broker.tend-us.tendplatform.com", HOST]

    names.clear()

    async def untrusted(host, port, *, ssl, server_hostname):
        names.append(server_hostname)
        error = tend_channel.ssl.SSLCertVerificationError("untrusted")
        error.verify_code = 20
        raise error

    monkeypatch.setattr(tend_channel.asyncio, "open_connection", untrusted)
    with pytest.raises(CannotConnect, match="certificate"):
        await TendChannel(lambda record: None)._open("vc.seedonk.com", 5101)
    assert names == ["vc.seedonk.com"]


def audio_message(audio, *, codec=4, source=b"owner:camera", combined=None):
    """0x0B, the source, then the codec byte and three, a length, four bytes, the audio."""
    combined = len(audio) + 4 if combined is None else combined
    data = bytes([codec, 0, 0, 0]) + struct.pack("<I", combined) + b"\x01\x02\x03\x04" + audio
    return (
        bytes([0x0B, 0])
        + struct.pack(">I", len(source))
        + source
        + struct.pack(">I", len(data))
        + data
    )


def test_channel_audio_follows_the_server_relay_layout():
    sources = frozenset({"owner:camera"})
    assert channel_audio(audio_message(b"opus!"), sources) == (4, b"opus!")
    assert channel_audio(audio_message(b"\xff" * 160, codec=2), sources) == (2, b"\xff" * 160)
    assert channel_audio(audio_message(b"opus!", combined=5), sources) == (4, b"opus!")
    assert channel_audio(audio_message(b"opus!", source=b"OWNER:Camera"), sources)
    for bad in (
        audio_message(b"opus!", source=b"intruder"),
        audio_message(b"opus!", combined=40),
        audio_message(b"opus!")[:-1],
        media([b"\x00\x00\x00\x01\x65idr"]),  # Video.
        b"\x0b",
    ):
        assert channel_audio(bad, sources) is None


async def test_audio_channel_passes_on_the_cameras_audio_and_never_goes_silent(
    channel, monkeypatch
):
    receiver, reader, writer, records, ended, _opened = channel
    heard = []
    receiver._on_audio = lambda *frame: heard.append(frame)
    monkeypatch.setattr(tend_channel, "FRAME_TIMEOUT", 0)
    sleep = asyncio.sleep
    monkeypatch.setattr(tend_channel.asyncio, "sleep", lambda _delay: sleep(0))
    reader.feed_data(Frame(257).encode())
    await receiver.start(("ac.seedonk.com", 5101), SENDER, 42, {"owner:camera"}, join=JOIN_AUDIO)
    assert struct.unpack_from("<H", writer.write.call_args_list[0].args[0], 13)[0] == 509
    reader.feed_data(Frame(413, data=audio_message(b"opus!")).encode())
    reader.feed_data(Frame(414, data=media([b"\x00\x00\x00\x01\x65idr"])).encode())
    await until(lambda: receiver.unreadable >= 1)
    assert heard == [(4, b"opus!", None)] and records == []
    for _ in range(20):  # The upkeep loop runs; an audio channel has no silence limit.
        await sleep(0)
    assert not ended and not receiver.ended
    await receiver.close()
    assert receiver.ended

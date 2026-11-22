"""Mode-9 negotiation over synthetic authenticated Tend signaling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.myq.api import tend_legacy
from custom_components.myq.api.errors import AccessDenied, CannotConnect, ProtocolError
from custom_components.myq.api.tend import Frame, parse_properties, properties
from custom_components.myq.api.tend_legacy import LegacyTendSession


@pytest.fixture
def legacy(monkeypatch):
    session = LegacyTendSession()
    reader = asyncio.StreamReader()
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
    media = MagicMock(start=AsyncMock(), recv=AsyncMock(return_value=b"video"), close=AsyncMock())
    monkeypatch.setattr(tend_legacy, "TendRtp", lambda: media)

    async def connect(host, port):
        session._reader, session._writer = reader, writer

    monkeypatch.setattr(session, "_connect", connect)

    def feed(*frames):
        for frame in frames:
            reader.feed_data(frame.encode())

    def login(inventory="owner:camera,AES=1234567890abcdef+LIP=192.0.2.20"):
        feed(Frame(519, data=properties({"alias": "login-user", "device-info-list": inventory})))

    async def sent():
        result = []
        for call in writer.write.call_args_list:
            wire = asyncio.StreamReader()
            wire.feed_data(call.args[0])
            result.append(await Frame.read(wire))
        return result

    return session, media, login, feed, sent, writer


def offer(alias="owner", mode="9", metadata=b"rtp://relay.seedonk.com:5432", action=411):
    return Frame(action, options=metadata, data=f"accept-video-receive|{alias}|{mode}|".encode())


async def test_named_broker_redirect_preserves_tls_and_reaches_inventory(legacy):
    session, media, login, feed, _, _ = legacy
    connect = session._connect
    connections = []

    async def connected(host, port):
        connections.append((host, port))
        await connect(host, port)

    session._connect = connected
    feed(Frame(519, data=properties({"redirectHost": "broker.seedonk.com"})))
    login()
    feed(offer())
    try:
        await session.start("owner:camera", "token")
        assert connections == [("server.seedonk.com", 5104), ("broker.seedonk.com", 5104)]
        media.start.assert_awaited_once()
    finally:
        await session.close()


@pytest.mark.parametrize("host", ["attacker.invalid", "seedonk.com.attacker.invalid", "127.0.0.1"])
async def test_named_redirect_cannot_send_credentials_outside_tend(legacy, host):
    session, media, _, feed, sent, _ = legacy
    feed(Frame(519, data=properties({"redirectHost": host})))
    with pytest.raises(ProtocolError, match="trusted camera service"):
        await session.start("owner:camera", "token")
    assert len(await sent()) == 1
    media.start.assert_not_awaited()


@pytest.mark.parametrize("action", [411, 438])
async def test_exact_inventory_identity_opens_media_before_start_and_closes_mode9(legacy, action):
    session, media, login, feed, sent, writer = legacy
    login()
    feed(offer("another-owner"), offer(mode="12"), offer(action=action))

    async def rendezvous(*args):
        assert not any(frame.data.startswith(b"start-video") for frame in await sent())

    media.start.side_effect = rendezvous
    assert await session.start("owner:camera", "synthetic-token") == {}
    media.start.assert_awaited_once_with(
        ("relay.seedonk.com", 5432), "owner", "192.0.2.20", b"1234567890abcdef"
    )
    assert session._login_inventory == ""
    assert await session.recv() == b"video"
    await session.control("set-ptz", (("S", "0"),))
    await asyncio.gather(session.close(), session.close())
    frames = [frame for frame in await sent() if frame.action == 411]
    assert [frame.data for frame in frames] == [
        b"require-video-send|owner|9|",
        b"start-video-receive|owner|9|",
        b"modify-device|owner|QTY=3+FPS=20+SIZE=0+SAVE=0|",
        b"set-ptz|owner|S|0|",
        b"stop-video-receive|owner|9|",
    ]
    routes = [parse_properties(frame.options) for frame in frames]
    assert {route["CX_DSTID"] for route in routes} == {"owner:camera"}
    assert routes[0]["CorrelationID"] == routes[1]["CorrelationID"] == routes[-1]["CorrelationID"]
    media.close.assert_awaited_once()
    writer.close.assert_called_once()
    assert not session._tasks
    with pytest.raises(CannotConnect):
        await session.recv()
    with pytest.raises(ProtocolError, match="cannot be reused"):
        await session.start("owner:camera", "token")


@pytest.mark.parametrize(
    "inventory",
    [
        "other:camera,AES=1234567890abcdef",
        "owner:camera,AES=1234567890abcdef\nowner:camera,AES=1234567890abcdef",
        "owner:camera,AES=short",
        "owner:camera,AES=1234567890abcdef+AES=1234567890abcdef",
        "owner:camera,LIP=127.0.0.1",
        "owner:camera,LIP=invalid",
    ],
)
async def test_bad_or_ambiguous_registration_never_opens_media(legacy, inventory):
    session, media, login, _, sent, writer = legacy
    login(inventory)
    with pytest.raises(ProtocolError):
        await session.start("owner:camera", "token")
    media.start.assert_not_awaited()
    assert not any(frame.action == 411 for frame in await sent())
    writer.close.assert_called_once()
    assert session._login_inventory == ""


@pytest.mark.parametrize(
    "metadata",
    [
        b"no relay",
        b"rtp://relay:0",
        b"rtp://relay:65536",
        b"rtp://-bad:4444",
        b"rtp://127.0.0.1:4444",
        b"rtp://relay:4444 rtp://other:4444",
    ],
)
async def test_invalid_offer_cleans_up(legacy, metadata):
    session, media, login, feed, sent, writer = legacy
    login()
    feed(offer(metadata=metadata))
    with pytest.raises(ProtocolError):
        await session.start("owner:camera", "token")
    media.start.assert_not_awaited()
    assert [frame.data for frame in await sent() if frame.action == 411][-1] == (
        b"stop-video-receive|owner|9|"
    )
    writer.close.assert_called_once()


async def test_denied_offer_is_not_retried(legacy):
    session, media, login, feed, sent, _ = legacy
    login()
    feed(Frame(411, data=b"deny-video-receive|owner|9|"))
    with pytest.raises(AccessDenied):
        await session.start("owner:camera", "token")
    media.start.assert_not_awaited()
    assert sum(frame.data.startswith(b"require-video") for frame in await sent()) == 1


@pytest.mark.parametrize("action", [411, 438])
async def test_late_denial_ends_the_matching_owner_session(legacy, action):
    session, _, login, feed, _, _ = legacy
    login()
    feed(offer())
    await session.start("owner:camera", "token")
    feed(Frame(action, data=b"deny-video-receive|another-owner|9|"))
    feed(Frame(action, data=b"deny-video-receive|owner|9|"))
    try:
        async with asyncio.timeout(1):
            with pytest.raises(AccessDenied):
                await session.wait_closed()
    finally:
        await session.close()


async def test_udp_failure_closes_signaling(legacy):
    session, media, login, feed, _, writer = legacy
    login()
    feed(offer())
    media.start.side_effect = CannotConnect("synthetic UDP failure")
    with pytest.raises(CannotConnect):
        await session.start("owner:camera", "token")
    media.close.assert_awaited_once()
    writer.close.assert_called_once()
    assert not session._tasks


async def test_cancellation_during_teardown_still_releases_udp_and_tls(legacy):
    session, media, login, feed, _, writer = legacy
    login()
    feed(offer())
    await session.start("owner:camera", "token")
    entered, release = asyncio.Event(), asyncio.Event()

    async def close_media():
        entered.set()
        await release.wait()

    media.close.side_effect = close_media
    task = asyncio.create_task(session.close())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await session.close()
    media.close.assert_awaited_once()
    writer.close.assert_called_once()


async def test_cancelled_start_releases_resources(legacy):
    session, media, login, _, _, writer = legacy
    login()
    requested = asyncio.Event()
    original = session._command

    async def command(*args, **kwargs):
        await original(*args, **kwargs)
        requested.set()

    session._command = command
    task = asyncio.create_task(session.start("owner:camera", "token"))
    await requested.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    writer.close.assert_called_once()
    assert session._login_inventory == ""

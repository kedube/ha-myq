"""Mode-9 negotiation over synthetic authenticated Tend signaling."""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.myq.api import tend, tend_legacy
from custom_components.myq.api.errors import AccessDenied, CannotConnect, ProtocolError
from custom_components.myq.api.tend import Frame, parse_properties, properties
from custom_components.myq.api.tend_legacy import (
    LegacyTendSession,
    _camera_registration,
    _relay,
    _texts,
)

INVENTORY = "owner:camera,AES=1234567890abcdef+LIP=192.168.1.20+GSN=OPENER-1"
# The front door's answer as the service sends it: both forms, and the block repeated.
FRONT_DOOR = (
    b"#Sun Nov 22 20:58:11 UTC 2026\n1=192.0.2.21\\:5104\n"
    b"redirectHost=b159.broker.tend-us.tendplatform.com\n"
) * 2


@pytest.fixture
async def legacy(monkeypatch):
    # Async, so the stream reader belongs to the test's own event loop.
    session = LegacyTendSession({"OPENER-1"})
    reader = asyncio.StreamReader()
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
    receivers = []

    def receiver(on_record, on_end):
        media = MagicMock(start=AsyncMock(), close=AsyncMock())
        media.on_record, media.on_end = on_record, on_end
        receivers.append(media)
        return media

    monkeypatch.setattr(tend_legacy, "TendRtp", receiver)
    monkeypatch.setattr(tend_legacy, "RETRY_DELAY", 0)
    connections = []

    async def connect(host, port):
        connections.append((host, port))
        session._reader, session._writer = reader, writer

    monkeypatch.setattr(session, "_connect", connect)

    def feed(*frames):
        for frame in frames:
            reader.feed_data(frame if isinstance(frame, bytes) else frame.encode())

    def login(inventory=INVENTORY, **extra):
        feed(Frame(519, data=properties({"sid": "s1", "device-info-list": inventory, **extra})))

    async def sent():
        result = []
        for call in writer.write.call_args_list:
            wire = asyncio.StreamReader()
            wire.feed_data(call.args[0])
            result.append(await Frame.read(wire))
        return result

    class Harness:
        pass

    harness = Harness()
    harness.session, harness.receivers, harness.connections = session, receivers, connections
    harness.login, harness.feed, harness.sent, harness.writer = login, feed, sent, writer
    harness.reader = reader
    harness.receiver = lambda factory: monkeypatch.setattr(tend_legacy, "TendRtp", factory)
    return harness


def accept(alias="owner", metadata=b"url=rtp\\://relay.seedonk.com\\:5432\n", action=411):
    return Frame(action, options=metadata, data=f"accept-video-receive|{alias}|9|".encode())


async def commands(harness):
    return [frame for frame in await harness.sent() if frame.action == 411]


async def test_login_follows_the_front_door_and_presents_the_mode9_client(legacy):
    legacy.feed(Frame(519, options=b"clear=clear\n", data=FRONT_DOOR))
    legacy.login()
    legacy.feed(accept())
    try:
        await legacy.session.start("owner:camera", "synthetic-token")
        assert legacy.connections == [
            (tend.HOST, 5104),
            ("b159.broker.tend-us.tendplatform.com", 5104),
        ]
        logins = [frame for frame in await legacy.sent() if frame.action == 519]
        assert len(logins) == 2
        profile = parse_properties(logins[1].options)
        assert profile["CX_PASSWD"] == "myQ.accessToken:synthetic-token"
        assert profile["sdk_version"] == "2.1.0.56" and profile["partner-id"] == "myQ"
        assert parse_properties(logins[1].data) == {"CX_UNAME": "", "mode": "0"}
    finally:
        await legacy.session.close()


async def test_session_answer_is_found_behind_other_frames_and_is_never_a_redirect(legacy):
    legacy.feed(
        Frame(503, data=b"\xff\xfe binary push"),
        Frame(519, data=b"note=not yet\n"),
        # A numbered key beside a session is data, not another redirect.
        Frame(
            519,
            sender=b"R" * 16,
            recipient=77,
            data=properties({"sid": "s1", "1": "x:1", "device-info-list": INVENTORY}),
        ),
        accept(),
    )
    await legacy.session.start("owner:camera", "token")
    try:
        assert legacy.connections == [(tend.HOST, 5104)]
        frames = await commands(legacy)
        # The broker's routing block is echoed; the header's routing field stays zero.
        assert {frame.sender for frame in frames} == {b"R" * 16}
        assert {frame.recipient for frame in frames} == {0}
    finally:
        await legacy.session.close()


async def test_rejected_login_and_missing_session_fail_without_media(legacy):
    legacy.feed(Frame(1, options=struct.pack("<I", 502)))
    with pytest.raises(AccessDenied, match="login was rejected"):
        await legacy.session.start("owner:camera", "token")
    assert not legacy.receivers
    legacy.writer.close.assert_called()


async def test_video_request_start_and_stop_use_one_correlation(legacy):
    legacy.login()
    legacy.feed(accept("someone-else-entirely"))
    records = []
    legacy.session.on_video = records.append

    before_rendezvous = []

    def receiver(on_record, on_end):
        media = MagicMock(close=AsyncMock())
        media.on_record = on_record

        async def rendezvous(*args):
            before_rendezvous.extend(frame.data for frame in await commands(legacy))

        media.start = AsyncMock(side_effect=rendezvous)
        legacy.receivers.append(media)
        return media

    legacy.receiver(receiver)
    assert await legacy.session.start("owner:camera", "token") == {}
    # The camera is asked to start only once the UDP rendezvous is complete.
    assert before_rendezvous == [b"require-video-send|owner|9|"]
    media = legacy.receivers[0]
    media.start.assert_awaited_once_with(
        ("relay.seedonk.com", 5432), "owner", "192.168.1.20", b"1234567890abcdef"
    )
    assert legacy.session._inventory == ""
    media.on_record(b"record")
    assert records == [b"record"]
    await asyncio.gather(legacy.session.close(), legacy.session.close())
    media.on_record(b"late")
    assert records == [b"record"]
    frames = await commands(legacy)
    assert [frame.data for frame in frames] == [
        b"require-video-send|owner|9|",
        b"start-video-receive|owner|9|",
        b"modify-device|owner|QTY=3+FPS=20+SIZE=0+SAVE=0|",
        b"stop-video-receive|owner|9|",
    ]
    routes = [parse_properties(frame.options) for frame in frames]
    assert {route["CX_DSTID"] for route in routes} == {"owner:camera"}
    correlation = routes[0]["CorrelationID"]
    assert correlation.startswith("LOG-")
    assert routes[1]["CorrelationID"] == routes[3]["CorrelationID"] == correlation
    assert routes[2]["CorrelationID"] != correlation
    assert (await legacy.sent())[-1].action == 408
    media.close.assert_awaited_once()
    legacy.writer.close.assert_called_once()
    assert not legacy.session._tasks
    with pytest.raises(ProtocolError, match="cannot be reused"):
        await legacy.session.start("owner:camera", "token")


async def test_first_rendezvous_miss_is_retried_once_with_a_fresh_request(legacy):
    legacy.login()
    legacy.feed(accept())
    attempts = 0

    def receiver(on_record, on_end):
        nonlocal attempts
        attempts += 1
        media = MagicMock(close=AsyncMock())
        if attempts == 1:
            media.start = AsyncMock(side_effect=CannotConnect("UDP timed out"))
        else:
            media.start = AsyncMock()
        legacy.receivers.append(media)
        if attempts == 1:
            # The camera accepts the second request only once it is made.
            asyncio.get_running_loop().call_later(0.05, legacy.feed, accept())
        return media

    legacy.receiver(receiver)
    await legacy.session.start("owner:camera", "token")
    try:
        assert attempts == 2
        frames = await commands(legacy)
        assert [frame.data.split(b"|")[0] for frame in frames] == [
            b"require-video-send",
            b"stop-video-receive",
            b"require-video-send",
            b"start-video-receive",
            b"modify-device",
        ]
        ids = [parse_properties(frame.options)["CorrelationID"] for frame in frames]
        assert ids[0] == ids[1] != ids[2] == ids[3]
        legacy.receivers[0].close.assert_awaited()
    finally:
        await legacy.session.close()


async def test_two_rendezvous_misses_end_the_session(legacy):
    legacy.login()
    legacy.feed(accept())

    def receiver(on_record, on_end):
        media = MagicMock(
            start=AsyncMock(side_effect=CannotConnect("UDP timed out")), close=AsyncMock()
        )
        if not legacy.receivers:
            asyncio.get_running_loop().call_later(0.05, legacy.feed, accept())
        legacy.receivers.append(media)
        return media

    legacy.receiver(receiver)
    with pytest.raises(CannotConnect, match="UDP timed out"):
        async with asyncio.timeout(2):
            await legacy.session.start("owner:camera", "token")
    assert len(legacy.receivers) == 2
    assert not any(f.data.startswith(b"start-video") for f in await commands(legacy))
    legacy.writer.close.assert_called_once()


async def test_denial_binary_pushes_and_other_cameras_denials(legacy):
    legacy.login()
    legacy.feed(
        Frame(410, data=b"\xff\xfe\x00 not text"),
        Frame(999, data=b"\x80\x81 unknown action, not text either"),
        Frame(411, data=b"deny-video-receive|another-owner|9|"),
        Frame(411, data=b"deny-video-receive|owner|9|"),
    )
    with pytest.raises(AccessDenied, match="declined live video"):
        async with asyncio.timeout(2):
            await legacy.session.start("owner:camera", "token")
    assert not legacy.receivers
    assert [f.data.split(b"|")[0] for f in await commands(legacy)] == [
        b"require-video-send",
        b"stop-video-receive",
    ]


async def test_acceptance_inside_a_batched_push_names_its_relay(legacy):
    text = b"accept-video-receive|owner|9|rtp://203.0.113.9:6000|"
    other = b"status|someone|1|"
    batch = b"".join(
        bytes([kind]) + len(item).to_bytes(4, "little") + item
        for kind, item in ((1, b"\x00binary"), (0, other), (0, text))
    )
    legacy.login()
    legacy.feed(Frame(501, data=batch + b"\x00\xff\xff\xff\x7f"))
    await legacy.session.start("owner:camera", "token")
    try:
        assert legacy.receivers[0].start.await_args.args[0] == ("203.0.113.9", 6000)
    finally:
        await legacy.session.close()
    assert _texts(Frame(501, data=batch)) == [other, text]
    assert _texts(Frame(411)) == []


async def test_no_acceptance_times_out(legacy, monkeypatch):
    monkeypatch.setattr(tend_legacy, "OFFER_TIMEOUT", 0.05)
    legacy.login()
    with pytest.raises(CannotConnect, match="failed or timed out"):
        await legacy.session.start("owner:camera", "token")
    assert not legacy.receivers
    legacy.writer.close.assert_called_once()


async def test_signaling_loss_kick_out_and_media_failure_end_the_session(legacy):
    legacy.login()
    legacy.feed(accept())
    seen = []
    legacy.session.on_status = lambda: seen.append("status")
    await legacy.session.start("owner:camera", "token")
    legacy.feed(
        Frame(411, data=b"status|other:camera|1|"),
        Frame(411, data=b"status|owner:camera|1|"),
        Frame(411, data=b"accept-video-receive|owner|9|"),  # Unrequested: ignored.
    )
    await asyncio.sleep(0.01)
    assert seen == ["status"]
    legacy.feed(Frame(411, data=b"kick-out|"))
    with pytest.raises(AccessDenied):
        async with asyncio.timeout(2):
            await legacy.session.wait_closed()
    await legacy.session.close()


async def test_keepalive_follows_an_interval_and_its_failure_ends_the_session(legacy, monkeypatch):
    monkeypatch.setattr(tend_legacy, "KEEPALIVE_INTERVAL", 0.02)
    legacy.login()
    legacy.feed(accept())
    await legacy.session.start("owner:camera", "token")
    assert not [frame for frame in await legacy.sent() if frame.action == 526]
    await asyncio.sleep(0.07)
    keepalives = [frame for frame in await legacy.sent() if frame.action == 526]
    assert len(keepalives) >= 2
    assert all(not frame.options and not frame.data for frame in keepalives)
    legacy.writer.drain.side_effect = ConnectionResetError
    with pytest.raises(CannotConnect, match="signaling disconnected"):
        async with asyncio.timeout(2):
            await legacy.session.wait_closed()
    legacy.writer.drain.side_effect = None
    await legacy.session.close()


async def test_media_failure_is_the_sessions_failure(legacy):
    legacy.login()
    legacy.feed(accept())
    await legacy.session.start("owner:camera", "token")
    legacy.receivers[0].on_end(CannotConnect("Tend camera stopped sending video"))
    with pytest.raises(CannotConnect, match="stopped sending video"):
        await legacy.session.wait_closed()
    await legacy.session.close()


async def test_closed_signaling_ends_the_session(legacy):
    legacy.login()
    legacy.feed(accept())
    await legacy.session.start("owner:camera", "token")
    legacy.reader.feed_eof()
    with pytest.raises(CannotConnect, match="signaling disconnected"):
        async with asyncio.timeout(2):
            await legacy.session.wait_closed()
    await legacy.session.close()


async def test_cancelled_start_releases_resources(legacy):
    legacy.login()
    legacy.feed(accept())
    entered = asyncio.Event()

    def receiver(on_record, on_end):
        async def start(*args):
            entered.set()
            await asyncio.Event().wait()

        media = MagicMock(start=AsyncMock(side_effect=start), close=AsyncMock())
        legacy.receivers.append(media)
        return media

    legacy.receiver(receiver)
    task = asyncio.create_task(legacy.session.start("owner:camera", "token"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    legacy.receivers[0].close.assert_awaited()
    legacy.writer.close.assert_called_once()
    assert not legacy.session._tasks


def test_camera_is_selected_by_its_alias_or_else_by_its_opener():
    two = (
        "owner:cam-a,AES=1234567890abcdef+LIP=192.168.1.20+GSN=OPENER-1\n"
        "owner:cam-b,LIP=not-an-address+GSN=OPENER-2\n"
        "junk-without-fields\nnocolon,GSN=OPENER-1"
    )
    assert _camera_registration(two, "owner:cam-a") == (
        "owner:cam-a",
        "owner",
        "192.168.1.20",
        b"1234567890abcdef",
    )
    # An alias Tend does not list falls back to the row naming this camera's opener.
    assert _camera_registration(two, "owner:other-spelling", {"OPENER-2", "GATEWAY-9"}) == (
        "owner:cam-b",
        "owner",
        "",
        b"",
    )
    # The camera's own row wins even when another row names one of its serials.
    assert _camera_registration(two, "owner:cam-a", {"OPENER-2"})[0] == "owner:cam-a"


@pytest.mark.parametrize(
    ("inventory", "openers", "message"),
    [
        ("", (), "0 listed"),
        ("owner:other,GSN=OPENER-9", ("OPENER-1",), "1 listed"),
        ("owner:other,GSN=", ("",), "not uniquely"),
        ("owner:camera,LIP=1.2.3.4\nowner:camera,LIP=1.2.3.5", (), "2 listed"),
        ("a:one,GSN=OPENER-1\nb:two,GSN=OPENER-1", ("OPENER-1",), "not uniquely"),
        ("owner:camera,AES=short", (), "media registration"),
        ("owner:camera,AES=" + "é" * 16, (), "media registration"),
        ("ownér:camera,GSN=x", (), "camera identity"),
    ],
)
def test_missing_ambiguous_or_invalid_registration_is_refused(inventory, openers, message):
    destination = "ownér:camera" if "ownér" in inventory else "owner:camera"
    with pytest.raises(ProtocolError, match=message):
        _camera_registration(inventory, destination, openers)


async def test_unlisted_camera_never_requests_video(legacy):
    legacy.login("owner:another,GSN=OPENER-9")
    with pytest.raises(ProtocolError, match="not uniquely listed"):
        await legacy.session.start("owner:camera", "token")
    assert not await commands(legacy)
    assert not legacy.receivers


def test_relay_is_the_first_endpoint_named():
    assert _relay(b"a=rtp\\://relay.seedonk.com\\:5432\nb=rtp://other.example:1") == (
        "relay.seedonk.com",
        5432,
    )
    assert _relay(b"", b"x|rtp://10.1.2.3:99|") == ("10.1.2.3", 99)
    assert _relay(b"url=rtp://relay.seedonk.com:5432\n") == ("relay.seedonk.com", 5432)


@pytest.mark.parametrize(
    "metadata",
    [b"", b"rtsp://relay:1", b"rtp://relay:0", b"rtp://relay:70000", b"rtp://-bad.example:5"]
    + [b"rtp://" + host + b":5" for host in (b"127.0.0.1", b"0.0.0.0", b"224.0.0.1", b"a..b")],
)
def test_unusable_relay_is_refused(metadata):
    with pytest.raises(ProtocolError, match="video relay"):
        _relay(metadata)


def test_camera_row_survives_separators_that_are_not_line_ends():
    """str.splitlines() would cut the row at these and lose the key and address."""
    for separator in ("\x0b", "\x0c", "\x1c", "\x85", "\u2028"):
        inventory = f"owner:camera,NAME=Front{separator}door+AES=1234567890abcdef+LIP=192.168.1.20"
        assert _camera_registration(inventory, "owner:camera") == (
            "owner:camera",
            "owner",
            "192.168.1.20",
            b"1234567890abcdef",
        )


@pytest.mark.parametrize("host", [b"169.254.10.20", b"255.255.255.255", b"240.0.0.1"])
def test_link_local_and_reserved_relay_addresses_are_refused(host):
    with pytest.raises(ProtocolError, match="video relay"):
        _relay(b"rtp://" + host + b":5")


async def test_login_answer_is_read_within_one_bounded_run_of_frames(legacy):
    """The wait for a session used to nest two loops and read up to 25 frames."""
    for _ in range(tend_legacy.LOGIN_FRAMES):
        legacy.feed(Frame(503, data=b"push"))
    legacy.login()
    with pytest.raises(ProtocolError, match="did not return a session"):
        await legacy.session.start("owner:camera", "token")
    assert not legacy.receivers

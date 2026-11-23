"""Synthetic Cxnet signaling fixtures and a real local TLS viewer session."""

import asyncio
import contextlib
import json
import ssl
import struct
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from custom_components.myq.api import tend
from custom_components.myq.api.errors import (
    AccessDenied,
    CannotConnect,
    CommandRejected,
    ProtocolError,
)
from custom_components.myq.api.tend import (
    Frame,
    TendSession,
    TendStatusSession,
    parse_properties,
    properties,
)


def wire(action, *, sender=b"\x19" * 16, recipient=42, options=b"", data=b""):
    """Fixture uses the 27-byte layout independently of Frame.encode()."""
    return (
        struct.pack(
            "<BBHHBIHHH2sQ",
            65,
            3,
            43 + len(options),
            16,
            0,
            len(data),
            121,
            action,
            2,
            b"**",
            recipient,
        )
        + sender
        + options
        + data
    )


def stream(*payloads):
    reader = asyncio.StreamReader()
    for payload in payloads:
        reader.feed_data(payload)
    return reader


def fake_session(monkeypatch, *responses):
    session = TendSession()
    reader = stream(*responses)
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    connections = []

    async def connect(host, port):
        connections.append((host, port))
        session._reader, session._writer = reader, writer

    monkeypatch.setattr(session, "_connect", connect)
    return session, reader, writer, connections


async def commands(writer):
    return [await Frame.read(stream(call.args[0])) for call in writer.write.call_args_list]


async def test_wire_header_and_fragmented_receive():
    packet = Frame(526).encode()
    assert packet[:27].hex() == "41032b001000000000000079000e0202002a2a0000000000000000"
    reader = asyncio.StreamReader()
    read = asyncio.create_task(Frame.read(reader))
    data = wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|cam|12|")
    for offset in range(0, len(data), 3):
        reader.feed_data(data[offset : offset + 3])
        await asyncio.sleep(0)
    result = await read
    assert result.action == 438
    assert result.sender == b"\x19" * 16
    assert result.recipient == 42
    assert result.options == b'{"kvs":{}}'


@pytest.mark.parametrize(
    "offset,value",
    [
        (0, b"B"),
        (1, b"\x09"),
        (2, b"\x01\x00"),
        (4, b"\xff\xff"),
        (7, b"\xff" * 4),
        (15, b"\x01\x00"),
    ],
)
async def test_malformed_headers_fail_before_reading_payload(offset, value):
    packet = bytearray(wire(519))
    packet[offset : offset + len(value)] = value
    with pytest.raises(ProtocolError, match="Invalid Tend signaling frame"):
        await Frame.read(stream(packet[:27]))


def test_java_properties_escape_and_continuation():
    assert parse_properties(
        b"#comment\nalias : demo\\ user\npath=x\\:\\=y\nmsg=first\\\n second\nu=\\u00e9\n"
    ) == {
        "alias": "demo user",
        "path": "x:=y",
        "msg": "firstsecond",
        "u": "é",
    }
    value = {"alias": "name:with=punctuation", "unicode": "café 😀", "line": "one\ntwo"}
    assert parse_properties(properties(value)) == value


async def test_start_routes_and_stops_using_alias_not_serial(monkeypatch):
    session, reader, writer, connections = fake_session(
        monkeypatch,
        wire(519, data=b"alias=user-alias\n"),
        wire(257),
        wire(
            438,
            options=b'{"kvs":{"region":"us-east-1"}}',
            data=b"accept-video-receive|other-camera|12|",
        ),
        wire(
            438,
            options=b'{"kvs":{"region":"us-east-1"}}',
            data=b"accept-video-receive|device-alias|12|",
        ),
    )
    assert await session.start("device-alias", "secret-token") == {"kvs": {"region": "us-east-1"}}
    await asyncio.sleep(0)
    await asyncio.gather(session.close(), session.close())
    sent = await commands(writer)
    assert connections == [("server.seedonk.com", 5104)]
    assert parse_properties(sent[0].options)["CX_PASSWD"] == "myQ.accessToken:secret-token"
    assert sent[1].data == b"require-video-send|user-alias|12|"
    assert parse_properties(sent[1].options)["CX_DSTID"] == "device-alias"
    assert sent[1].sender == b"\x19" * 16 and sent[1].recipient == 42
    assert [frame.action for frame in sent] == [519, 411, 526, 411, 408]
    assert sent[-2].data == b"stop-video-receive|user-alias|12|"
    assert sent[-1].options == b"\0" * 4
    writer.close.assert_called_once()
    assert session._tasks == []
    await session.wait_closed()
    with pytest.raises(ProtocolError, match="cannot be reused"):
        await session.start("device-alias", "another-token")


async def test_typed_controls_and_alias_scoped_status_push(monkeypatch):
    session, reader, writer, _ = fake_session(
        monkeypatch,
        wire(519, data=b"alias=user-alias\n"),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|camera-alias|12|"),
    )
    changed = asyncio.Event()
    session.on_status = changed.set
    await session.start("camera-alias", "token")
    await session.control("set-ptz", (("P", "-1"),))
    await session.control("trigger-siren-action", (("31", "02"),))
    sent = [item for item in await commands(writer) if item.action == 411]
    assert sent[-2].data == b"set-ptz|user-alias|P|-1|"
    assert sent[-1].data == b"trigger-siren-action|user-alias|31|02|"
    assert parse_properties(sent[-1].options)["CX_DSTID"] == "camera-alias"
    reader.feed_data(wire(411, data=b"status|other-camera|1|"))
    await asyncio.sleep(0)
    assert not changed.is_set()
    reader.feed_data(wire(411, data=b"status|camera-alias|1|"))
    async with asyncio.timeout(1):
        await changed.wait()
    with pytest.raises(CommandRejected):
        await session.control("set-ptz", (("P", "C"),))
    await session.close()
    with pytest.raises(CommandRejected):
        await session.control("set-ptz", (("S", "0"),))


async def test_uncertain_activation_allows_only_bounded_cleanup_stop(monkeypatch):
    session, _, writer, _ = fake_session(
        monkeypatch,
        wire(519, data=b"alias=user\n"),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|camera|12|"),
    )
    await session.start("camera", "token")
    # The write succeeds before drain fails: the physical result is unknowable.
    writer.drain.side_effect = [OSError("synthetic drain failure"), None, None]
    with pytest.raises(CannotConnect, match="not retried"):
        await session.control("trigger-siren-action", (("31", "01"),))
    assert session._closed.done()
    with pytest.raises(CommandRejected):
        await session.control("trigger-siren-action", (("31", "01"),), cleanup=True)
    with pytest.raises(CommandRejected):
        await session.control("set-ptz", (("P", "1"),), cleanup=True)
    await session.control("trigger-siren-action", (("31", "02"),), cleanup=True)
    sent = [frame.data for frame in await commands(writer) if frame.action == 411]
    assert sent == [
        b"require-video-send|user|12|",
        b"trigger-siren-action|user|31|01|",
        b"trigger-siren-action|user|31|02|",
    ]
    await session.close()


async def test_idle_status_feed_never_requests_video_or_sends_controls(monkeypatch):
    changed = []
    ready = asyncio.Event()
    session = TendStatusSession(
        {"camera-one", "camera-two"}, lambda alias: (changed.append(alias), ready.set())
    )
    reader = stream(wire(519, data=b"alias=user\n"))
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())

    async def connect(host, port):
        session._reader, session._writer = reader, writer

    monkeypatch.setattr(session, "_connect", connect)
    await session.subscribe("token")
    await asyncio.sleep(0)
    reader.feed_data(wire(411, data=b"status|unrelated-camera|1|"))
    reader.feed_data(wire(411, data=b"status|camera-two|0|"))
    async with asyncio.timeout(1):
        await ready.wait()
    assert changed == ["camera-two"]
    with pytest.raises(CommandRejected):
        await session.control("set-ptz", (("P", "1"),))
    with pytest.raises(CommandRejected):
        await session.control("trigger-siren-action", (("31", "02"),), cleanup=True)
    await session.close()
    assert [frame.action for frame in await commands(writer)] == [519, 526, 408]
    assert session._tasks == []


async def test_batched_denial_ends_start_without_replay(monkeypatch):
    denial = b"deny-video-receive|device-alias|not authorized|"
    batch = b"\0" + struct.pack("<I", len(denial)) + denial
    session, _, writer, _ = fake_session(
        monkeypatch, wire(519, data=b"alias=user\n"), wire(501, data=batch)
    )
    with pytest.raises(AccessDenied):
        await session.start("device-alias", "secret")
    sent = await commands(writer)
    assert [f.action for f in sent] == [519, 411, 411, 408]
    writer.close.assert_called_once()


async def test_timeout_and_cancellation_close_authenticated_session(monkeypatch):
    monkeypatch.setattr(tend, "HANDSHAKE_TIMEOUT", 0.01)
    session, _, writer, _ = fake_session(monkeypatch, wire(519, data=b"alias=user\n"))
    with pytest.raises(CannotConnect, match="timed out"):
        await session.start("device-alias", "secret")
    assert [f.action for f in await commands(writer)] == [519, 411, 411, 408]
    writer.close.assert_called_once()

    monkeypatch.setattr(tend, "HANDSHAKE_TIMEOUT", 5)
    session, _, writer, _ = fake_session(monkeypatch, wire(519, data=b"alias=user\n"))
    task = asyncio.create_task(session.start("device-alias", "secret"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [f.action for f in await commands(writer)] == [519, 411, 411, 408]
    writer.close.assert_called_once()


async def test_signaling_loss_notifies_viewer(monkeypatch):
    session, reader, writer, _ = fake_session(
        monkeypatch,
        wire(519, data=b"alias=user\n"),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|cam|12|"),
    )
    await session.start("cam", "secret")
    reader.feed_eof()
    with pytest.raises(CannotConnect, match="disconnected"):
        await session.wait_closed()
    await session.close()
    writer.close.assert_called_once()


@pytest.mark.parametrize(
    "redirect",
    [
        "evil.example:4104",
        "seedonk.com.evil.example:4104",
        "seedonk.com@evil.example:4104",
        "127.0.0.1:4104",
        "[::1]:4104",
        "foo.seedonk.com:0",
        "foo.seedonk.com:65536",
        "foo.seedonk.com/path:4104",
        "foo.seedonk.com#fragment:4104",
    ],
)
async def test_untrusted_redirect_never_receives_credentials(monkeypatch, redirect):
    session, _, writer, connections = fake_session(
        monkeypatch, wire(519, data=properties({"1": redirect}))
    )
    with pytest.raises(ProtocolError):
        await session.start("cam", "secret")
    assert connections == [(tend.HOST, tend.PORT)]
    assert len(await commands(writer)) == 1
    writer.close.assert_called_once()


async def test_trusted_hub_and_datacenter_redirect_keep_tls(monkeypatch):
    session, _, writer, connections = fake_session(
        monkeypatch,
        wire(519, data=b"1=hub.seedonk.com\\:4104\n"),
        wire(
            1,
            options=struct.pack("<I", 533),
            data=b"host=hub.tendplatform.com&port=4104&sslPort=5105",
        ),
        wire(519, data=b"alias=user\n"),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|cam|12|"),
    )
    await session.start("cam", "secret")
    await session.close()
    assert connections == [
        (tend.HOST, 5104),
        ("hub.seedonk.com", 5104),
        ("hub.tendplatform.com", 5105),
    ]
    assert [f.action for f in await commands(writer)].count(519) == 3
    assert writer.close.call_count == 3


async def test_redirect_loop_is_bounded(monkeypatch):
    session, _, writer, connections = fake_session(
        monkeypatch, *[wire(519, data=b"1=hub.seedonk.com:4104\n")] * 4
    )
    with pytest.raises(ProtocolError, match="too many times"):
        await session.start("cam", "secret")
    assert len(connections) == 4
    assert [f.action for f in await commands(writer)] == [519] * 4


@pytest.mark.parametrize("status_only", [False, True])
async def test_real_local_tls_signaling_session(monkeypatch, tmp_path, socket_enabled, status_only):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    received = []
    finished = asyncio.Event()

    async def serve(reader, writer):
        try:
            received.append(await Frame.read(reader))
            writer.write(wire(519, data=b"alias=user\n"))
            await writer.drain()
            # Fragment actual encrypted delivery, not just a mocked frame decoder.
            if status_only:
                response = wire(411, data=b"status|cam|1|")
            else:
                received.append(await Frame.read(reader))
                response = wire(
                    438,
                    options=json.dumps({"kvs": {"region": "us-east-1"}}).encode(),
                    data=b"accept-video-receive|cam|12|",
                )
            writer.write(response[:20])
            await writer.drain()
            writer.write(response[20:])
            await writer.drain()
            while True:
                frame = await Frame.read(reader)
                received.append(frame)
                if frame.action == 408:
                    break
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            finished.set()

    async with await asyncio.start_server(serve, "127.0.0.1", 0, ssl=server_context) as server:
        port = server.sockets[0].getsockname()[1]
        original = asyncio.open_connection

        async def local_connection(host, requested_port, **kwargs):
            assert host == tend.HOST and requested_port == tend.PORT
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
            assert kwargs["ssl"].check_hostname
            assert kwargs["server_hostname"] == tend.HOST
            return await original(
                "127.0.0.1", port, ssl=client_context, server_hostname="localhost"
            )

        monkeypatch.setattr(tend.asyncio, "open_connection", local_connection)
        if status_only:
            status_seen = asyncio.Event()
            session = TendStatusSession({"cam"}, lambda alias: status_seen.set())
            await session.subscribe("secret")
            async with asyncio.timeout(2):
                await status_seen.wait()
        else:
            session = TendSession()
            assert await session.start("cam", "secret") == {"kvs": {"region": "us-east-1"}}
        await session.close()
        await asyncio.wait_for(finished.wait(), 2)
    assert parse_properties(received[0].options)["CX_PASSWD"] == "myQ.accessToken:secret"
    if status_only:
        assert [frame.action for frame in received] == [519, 526, 408]
    else:
        assert received[1].data == b"require-video-send|user|12|"
        assert [frame.data for frame in received if frame.action == 411] == [
            b"require-video-send|user|12|",
            b"stop-video-receive|user|12|",
        ]
    assert received[-1].action == 408


async def test_tls_context_is_loaded_once_off_loop_and_verifies(monkeypatch):
    monkeypatch.setattr(tend, "_tls_context", None)
    real = ssl.create_default_context
    calls = 0

    def create_default_context(*args, **kwargs):
        nonlocal calls
        calls += 1
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()  # CA loading must run in a worker thread.
        return real(*args, **kwargs)

    monkeypatch.setattr(tend.ssl, "create_default_context", create_default_context)
    first = await tend._client_tls_context()
    second = await tend._client_tls_context()
    assert first is second and calls == 1
    assert first.verify_mode == ssl.CERT_REQUIRED and first.check_hostname


async def test_stop_reuses_the_view_correlation_id(monkeypatch):
    session, reader, writer, _ = fake_session(
        monkeypatch,
        wire(519, data=b"alias=user-alias\n"),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|device-alias|12|"),
    )
    await session.start("device-alias", "secret-token")
    await session.close()
    sent = await commands(writer)
    start = next(frame for frame in sent if frame.data.startswith(b"require-video-send"))
    stop = next(frame for frame in sent if frame.data.startswith(b"stop-video-receive"))
    # f81/b: one CorrelationID per view, carried by its stop as well.
    assert (
        parse_properties(start.options)["CorrelationID"]
        == parse_properties(stop.options)["CorrelationID"]
    )


@pytest.mark.parametrize("action", [1, 310])
async def test_isolated_naks_do_not_end_the_session(monkeypatch, action):
    # eg1/b turns these into NAKs; ConnectionManager drops only after more than 10.
    session, reader, writer, _ = fake_session(
        monkeypatch,
        wire(519, data=b"alias=user-alias\n"),
        wire(action),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|device-alias|12|"),
    )
    assert await session.start("device-alias", "secret-token") == {"kvs": {}}
    for _ in range(10):
        reader.feed_data(wire(action))
    await asyncio.sleep(0.01)
    assert session.connected
    reader.feed_data(wire(action))
    with pytest.raises(AccessDenied):
        await asyncio.wait_for(session.wait_closed(), 1)
    await session.close()


async def test_command_session_sends_only_typed_commands(monkeypatch):
    session = tend.TendCommandSession()
    reader = stream(wire(519, data=b"alias=user-alias\n"))
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    async def connect(host, port):
        session._reader, session._writer = reader, writer

    monkeypatch.setattr(session, "_connect", connect)
    await session.open("secret-token")
    await session.send_to("camera-alias", "set-ptz", (("G", "3"),))
    await session.send_to("camera-alias", "trigger-siren-action", (("31", "01"),))
    for bad in ((("G", "x"),), (("P", "1"),), (("G", "1"), ("G", "2"))):
        with pytest.raises(CommandRejected):
            await session.send_to("camera-alias", "set-ptz", bad)
    await session.close()
    sent = await commands(writer)
    moves = [frame for frame in sent if frame.action == 411]
    assert [frame.data for frame in moves] == [
        b"set-ptz|user-alias|G|3|",
        b"trigger-siren-action|user-alias|31|01|",
    ]
    assert parse_properties(moves[0].options)["CX_DSTID"] == "camera-alias"
    # No view was requested, so only the logout follows.
    assert [frame.action for frame in sent][-1] == 408
    assert not any(frame.data.startswith(b"stop-video-receive") for frame in sent)


async def test_broker_certificate_is_checked_against_the_front_door_name(monkeypatch):
    """Every broker serves the front door's certificate, so its own name never matches."""
    reader = stream(
        wire(
            519,
            options=b"clear=clear\n",
            data=b"1=192.0.2.21\\:5104\nredirectHost=b159.broker.tend-us.tendplatform.com\n" * 2,
        ),
        wire(519, data=b"alias=user\n"),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|cam|12|"),
    )
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
    opened = []

    async def open_connection(host, port, **kwargs):
        opened.append((host, port, kwargs["server_hostname"]))
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED and kwargs["ssl"].check_hostname
        return reader, writer

    monkeypatch.setattr(tend.asyncio, "open_connection", open_connection)
    session = TendSession()
    await session.start("cam", "secret")
    await session.close()
    assert opened == [
        (tend.HOST, 5104, tend.HOST),
        ("b159.broker.tend-us.tendplatform.com", 5104, tend.HOST),
    ]


async def test_login_answer_is_found_behind_unrelated_broker_frames(monkeypatch):
    session, _, writer, _ = fake_session(
        monkeypatch,
        wire(503, data=b"\xff\xfe binary"),
        wire(411, data=b"status|other|1|"),
        wire(519, data=b"alias=user\n"),
        wire(438, options=b'{"kvs":{}}', data=b"accept-video-receive|cam|12|"),
    )
    assert await session.start("cam", "secret") == {"kvs": {}}
    await session.close()
    assert [frame.action for frame in await commands(writer)][:2] == [519, 411]


async def test_login_gives_up_when_no_answer_follows(monkeypatch):
    session, _, writer, _ = fake_session(
        monkeypatch, *[wire(411, data=b"status|other|1|")] * tend.MAX_LOGIN_FRAMES
    )
    with pytest.raises(ProtocolError, match="Unexpected Tend login response"):
        await session.start("cam", "secret")
    assert [frame.action for frame in await commands(writer)] == [519]
    writer.close.assert_called_once()


async def test_tls_context_adds_the_bundled_authorities(monkeypatch):
    monkeypatch.setattr(tend, "_tls_context", None)
    loaded = []
    real = ssl.SSLContext.load_verify_locations

    def load(self, cafile=None, capath=None, cadata=None):
        loaded.append(cafile)
        return real(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", load)
    context = await tend._client_tls_context()
    import certifi

    assert certifi.where() in loaded
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize(
    "answer",
    [
        wire(519, data="1=hub.seedonk.com\\:\u00b2\n".encode("iso-8859-1")),
        wire(
            1,
            options=struct.pack("<I", 533),
            data="host=hub.seedonk.com\nsslPort=\u00b2\n".encode("iso-8859-1"),
        ),
    ],
)
async def test_redirect_port_of_digits_python_cannot_read_is_a_protocol_error(monkeypatch, answer):
    """ "²".isdigit() is true, yet int("²") raises: that must not escape as a ValueError."""
    session, _, writer, connections = fake_session(monkeypatch, answer)
    with pytest.raises(ProtocolError, match="redirect"):
        await session.start("cam", "secret")
    assert len(connections) == 1
    writer.close.assert_called_once()


async def test_push_that_is_not_text_does_not_end_the_status_session(monkeypatch):
    changed = []
    session = TendStatusSession({"camera-one"}, changed.append)
    reader = stream(wire(519, data=b"alias=user\n"))
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())

    async def connect(host, port):
        session._reader, session._writer = reader, writer

    monkeypatch.setattr(session, "_connect", connect)
    await session.subscribe("token")
    reader.feed_data(wire(999, data=b"\x80\x81 not utf-8"))
    reader.feed_data(wire(438, options=b"\xff", data=b"\xfe"))
    reader.feed_data(wire(501, data=b"\x00\x03\x00\x00\x00\xff\xfe\xfd"))
    reader.feed_data(wire(411, data=b"status|camera-one|1|"))
    async with asyncio.timeout(1):
        while not changed:  # noqa: ASYNC110
            await asyncio.sleep(0.005)
    assert session.connected and changed == ["camera-one"]
    await session.close()


def test_property_values_are_cut_only_at_real_line_ends():
    """Java ends a line at \\n, \\r or \\r\\n; a value may hold other separators."""
    assert parse_properties(b"a=one\x0ctwo\nb=x\x85y\r\nc=3\rd=4") == {
        "a": "one\x0ctwo",
        "b": "x\x85y",
        "c": "3",
        "d": "4",
    }

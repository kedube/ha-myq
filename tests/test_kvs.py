"""Mode-12 signaling contracts and real local WebSocket lifecycle checks."""

import asyncio
import base64
import json
import ssl
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from custom_components.myq.api.errors import CannotConnect, ProtocolError
from custom_components.myq.api.kvs import KvsConfig, KvsSession


@pytest.fixture
def kvs_options():
    return {
        "kvs": {
            "channelUrl": "wss://example.kinesisvideo.us-east-1.amazonaws.com/?signed=secret",
            "channelArn": "arn:aws:kinesisvideo:us-east-1:123:channel/example/1",
            "channelName": "example",
            "region": "us-east-1",
            "endpoints": [],
            "iceServers": [
                {"urls": ["stun:stun.kinesisvideo.us-east-1.amazonaws.com:443"]},
                {
                    "urls": ["turn:example.kinesisvideo.us-east-1.amazonaws.com:443?transport=udp"],
                    "username": "private-user",
                    "password": "private-password",
                    "ttl": 300,
                },
            ],
        }
    }


def test_handshake_config_parses_temporary_ice_material_and_hides_it(kvs_options):
    config = KvsConfig.from_server_options(json.dumps(kvs_options))
    assert config.ice_servers[1].password == "private-password"
    assert config.ice_servers[1].ttl == 300
    assert config.expires_at is not None
    assert "secret" not in repr(config)
    assert "private" not in repr(config.ice_servers[1])
    assert config.region == "us-east-1"


@pytest.mark.parametrize(
    "url",
    [
        "ws://example.kinesisvideo.us-east-1.amazonaws.com/",
        "wss://example.kinesisvideo.us-east-1.amazonaws.com.evil.invalid/",
        "wss://example.kinesisvideo.us-west-2.amazonaws.com/",
        "wss://127.0.0.1/",
        "wss://user:password@example.kinesisvideo.us-east-1.amazonaws.com/",
        "wss://example.kinesisvideo.us-east-1.amazonaws.com:1234/",
        "wss://example.kinesisvideo.us-east-1.amazonaws.com/#private",
    ],
)
def test_bad_signaling_destination_rejected(kvs_options, url):
    kvs_options["kvs"]["channelUrl"] = url
    with pytest.raises(ProtocolError):
        KvsConfig.from_server_options(kvs_options)


@pytest.mark.parametrize("ttl", [0, -1, True, "300"])
def test_invalid_ice_lifetime_rejected(kvs_options, ttl):
    kvs_options["kvs"]["iceServers"][1]["ttl"] = ttl
    with pytest.raises(ProtocolError):
        KvsConfig.from_server_options(kvs_options)


def test_signed_url_lifetime_ignores_the_local_wall_clock(kvs_options, monkeypatch):
    # The app applies no expiry check; a skewed HA clock must not reject fresh links.
    kvs_options["kvs"]["channelUrl"] += "&X-Amz-Date=20000101T000000Z&X-Amz-Expires=299"
    config = KvsConfig.from_server_options(kvs_options)
    config.ensure_fresh()
    # The lifetime still counts from receipt, so a stale configuration is refused.
    monkeypatch.setattr("custom_components.myq.api.kvs.time.monotonic", lambda: 10.0**9)
    with pytest.raises(ProtocolError, match="expired"):
        config.ensure_fresh()


def test_signaling_payloads_decode_leniently():
    import base64
    import json as _json

    from custom_components.myq.api.kvs import _event

    body = base64.b64encode(_json.dumps({"type": "answer", "sdp": "v=0"}).encode()).decode()
    wrapped = "\n".join(body[i : i + 8] for i in range(0, len(body), 8)).rstrip("=")
    message = _json.dumps({"messageType": "SDP_ANSWER", "messagePayload": wrapped})
    assert _event(message) == {"type": "answer", "sdp": "v=0"}
    # KvsEventParser ignores messages without a payload.
    assert _event(_json.dumps({"messageType": "ICE_CANDIDATE"})) is None
    end = base64.b64encode(_json.dumps({"candidate": "", "sdpMid": "0"}).encode()).decode()
    assert (
        _event(_json.dumps({"messageType": "ICE_CANDIDATE", "messagePayload": end}))["candidate"]
        == ""
    )


@pytest.mark.parametrize(
    "server",
    [
        {"urls": ["turn:127.0.0.1:443"], "username": "x", "password": "x"},
        {"urls": ["https://example.kinesisvideo.us-east-1.amazonaws.com/"]},
        {"urls": ["turn:example.kinesisvideo.us-east-1.amazonaws.com:443"]},
        {"urls": []},
        {"urls": "stun:stun.kinesisvideo.us-east-1.amazonaws.com:443"},
    ],
)
def test_unusable_ice_configuration_rejected(kvs_options, server):
    kvs_options["kvs"]["iceServers"] = [server]
    with pytest.raises(ProtocolError):
        KvsConfig.from_server_options(kvs_options)


@pytest.fixture
def tls_server_context(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "key.pem", tmp_path / "cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


async def local_server(handler, context):
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
    return runner, f"wss://127.0.0.1:{runner.addresses[0][1]}"


def envelope(kind, data):
    return {
        "messageType": kind,
        "messagePayload": base64.b64encode(json.dumps(data).encode()).decode(),
    }


async def test_real_websocket_offer_answer_ice_and_cleanup(socket_enabled, tls_server_context):
    received = []
    headers = []
    raw_urls = []
    peer_closed = asyncio.Event()
    events = asyncio.Queue()
    errors = AsyncMock()

    async def handler(request):
        headers.append(dict(request.headers))
        raw_urls.append(request.raw_path)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            data = json.loads(message.data)
            data["decoded"] = json.loads(base64.urlsafe_b64decode(data["messagePayload"]))
            received.append(data)
            if data["action"] == "SDP_OFFER":
                await ws.send_json(envelope("SDP_ANSWER", {"type": "answer", "sdp": "v=0\r\n"}))
                await ws.send_json(
                    envelope(
                        "ICE_CANDIDATE",
                        {"candidate": "candidate:remote", "sdpMid": "0", "sdpMLineIndex": 0},
                    )
                )
        peer_closed.set()
        return ws

    runner, base = await local_server(handler, tls_server_context)
    # Construct directly for loopback tests; production parsing permits only AWS.
    config = KvsConfig(base + "/?signed=a%2Fb%2Bc", "us-east-1", ())
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            headers={"Authorization": "Bearer PRIVATE", "Cookie": "PRIVATE"},
        ) as shared:
            signaling = KvsSession(shared, config, events.put, errors)
            await signaling.open()
            await signaling.send_offer("v=0\r\n")
            await signaling.send_candidate(
                {"candidate": "candidate:local", "sdpMid": "0", "sdpMLineIndex": 0}
            )
            async with asyncio.timeout(3):
                assert await events.get() == {"type": "answer", "sdp": "v=0\r\n"}
                assert (await events.get())["candidate"] == "candidate:remote"
            await signaling.close()
            async with asyncio.timeout(3):
                await peer_closed.wait()
            assert not shared.closed
            assert not shared.connector.closed
            assert raw_urls == ["/?signed=a%2Fb%2Bc"]
            assert "Authorization" not in headers[0]
            assert "Cookie" not in headers[0]
            assert received[0]["decoded"] == {"type": "offer", "sdp": "v=0\r\n"}
            assert received[0]["recipientClientId"] == ""
            assert received[0]["senderClientId"].startswith("session.")
            assert received[1]["decoded"]["candidate"] == "candidate:local"
            errors.assert_not_called()
            with pytest.raises(CannotConnect):
                await signaling.send_offer("v=0\r\n")
    finally:
        await runner.cleanup()


async def test_redirect_not_followed_or_secret_exposed(socket_enabled, tls_server_context):
    requests = []

    async def handler(request):
        requests.append(request.path)
        raise web.HTTPFound(location="/must-not-be-followed?secret=PRIVATE")

    runner, base = await local_server(handler, tls_server_context)
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as shared:
            signaling = KvsSession(shared, KvsConfig(base, "us-east-1", ()), AsyncMock())
            with pytest.raises(CannotConnect) as caught:
                await signaling.open()
            assert "PRIVATE" not in str(caught.value)
            assert requests == ["/"]
            assert signaling._session is None
            assert not shared.closed
    finally:
        await runner.cleanup()


async def test_disconnected_handshake_is_not_replayed(socket_enabled, tls_server_context):
    requests = []

    async def handler(request):
        requests.append(request.path)
        request.transport.abort()
        return web.Response()

    runner, base = await local_server(handler, tls_server_context)
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as shared:
            signaling = KvsSession(shared, KvsConfig(base, "us-east-1", ()), AsyncMock())
            with pytest.raises(CannotConnect):
                await signaling.open()
            assert requests == ["/"]
            assert signaling._session is None
    finally:
        await runner.cleanup()


async def test_cancel_pending_handshake_closes_child_session(socket_enabled, tls_server_context):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        started.set()
        await release.wait()
        return web.Response(status=503)

    runner, base = await local_server(handler, tls_server_context)
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as shared:
            signaling = KvsSession(shared, KvsConfig(base, "us-east-1", ()), AsyncMock())
            opening = asyncio.create_task(signaling.open())
            async with asyncio.timeout(3):
                await started.wait()
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
            assert signaling._session is None
            assert not shared.closed
    finally:
        release.set()
        await runner.cleanup()


@pytest.mark.parametrize("payload", ["not-json-PRIVATE", json.dumps(envelope("SDP_ANSWER", {}))])
async def test_bad_remote_message_fails_safely_and_closes(
    socket_enabled, tls_server_context, payload
):
    peer_closed = asyncio.Event()
    errors = asyncio.Queue()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(payload)
        async for _message in ws:
            pass
        peer_closed.set()
        return ws

    runner, base = await local_server(handler, tls_server_context)
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as shared:
            callback = AsyncMock()
            signaling = KvsSession(shared, KvsConfig(base, "us-east-1", ()), callback, errors.put)
            await signaling.open()
            async with asyncio.timeout(3):
                error = await errors.get()
                await peer_closed.wait()
            assert isinstance(error, ProtocolError)
            assert "PRIVATE" not in str(error)
            callback.assert_not_called()
            assert signaling._session is None
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("address", ["?signed=a%2Fb", "/?signed=a%2Fb"])
async def test_signed_address_without_a_path_is_not_mistaken_for_a_redirect(
    socket_enabled, tls_server_context, address
):
    """The HTTP client writes "host?query" as "host/?query"; that is the same address."""
    raw_urls = []

    async def handler(request):
        raw_urls.append(request.raw_path)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    runner, base = await local_server(handler, tls_server_context)
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as shared:
            signaling = KvsSession(shared, KvsConfig(base + address, "us-east-1", ()), AsyncMock())
            await signaling.open()
            await signaling.close()
        # What was signed is what was sent.
        assert raw_urls == ["/?signed=a%2Fb"]
    finally:
        await runner.cleanup()


def test_empty_signaling_frame_is_skipped():
    from custom_components.myq.api.kvs import _event

    assert _event("") is None and _event(" \r\n") is None
    with pytest.raises(ProtocolError):
        _event("not-json")

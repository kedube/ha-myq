"""Transport failures remain useful without revealing authentication URLs."""

import ssl
import traceback
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.myq.api.errors import (
    BrowserVerificationRequired,
    CannotConnect,
    ProtocolError,
    RateLimited,
)
from custom_components.myq.api.http import Http

KEY = SimpleNamespace(host="private-host", port=443, ssl=True)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (
            aiohttp.ClientConnectorCertificateError(KEY, ssl.CertificateError("private-secret")),
            "TLS",
        ),
        (aiohttp.ClientConnectorDNSError(KEY, OSError(8, "private-secret")), "DNS"),
        (TimeoutError("private-secret"), "timeout"),
        (aiohttp.ClientConnectorError(KEY, OSError(61, "private-secret")), "OS error 61"),
        (aiohttp.ServerDisconnectedError("private-secret"), "ServerDisconnectedError"),
    ],
)
async def test_connection_errors_do_not_leak_urls_or_secrets(failure, expected):
    session = MagicMock()
    session.request.side_effect = failure
    with pytest.raises(CannotConnect) as caught:
        await Http(session).request("GET", "https://example.invalid/?token=private-secret")
    rendered = "".join(traceback.format_exception(caught.value))
    assert expected in str(caught.value)
    # Exclude source-code context from this test; exception chains must be hidden.
    assert caught.value.__suppress_context__
    assert "private-host" not in rendered
    assert "private-secret" not in str(caught.value)


async def chunks(*values):
    for value in values:
        yield value


async def test_retry_after_blocks_subsequent_requests():
    session = MagicMock()
    response = session.request.return_value.__aenter__.return_value
    response.status = 429
    response.headers = {"Retry-After": "120"}
    response.content.iter_chunked = MagicMock(return_value=chunks(b"private-body"))
    http = Http(session)
    with pytest.raises(RateLimited) as caught:
        await http.request("GET", "https://example.invalid")
    assert caught.value.retry_after == 120
    with pytest.raises(RateLimited):
        await http.request("GET", "https://example.invalid")
    session.request.assert_called_once()


async def test_oversized_response_is_rejected(monkeypatch):
    monkeypatch.setattr("custom_components.myq.api.http.MAX_RESPONSE", 4)
    session = MagicMock()
    response = session.request.return_value.__aenter__.return_value
    response.content.iter_chunked = MagicMock(return_value=chunks(b"123", b"45"))
    with pytest.raises(ProtocolError):
        await Http(session).request("GET", "https://example.invalid")


@pytest.mark.parametrize("status", [403, 429])
async def test_cloudflare_challenge_is_not_reported_as_rate_limit(status):
    session = MagicMock()
    response = session.request.return_value.__aenter__.return_value
    response.status = status
    response.headers = {"CF-Mitigated": "challenge", "Content-Type": "text/html"}
    response.content.iter_chunked = MagicMock(return_value=chunks(b"private-challenge"))
    http = Http(session)
    with pytest.raises(BrowserVerificationRequired):
        await http.request("GET", "https://example.invalid")
    http.backoff.check("https://example.invalid")  # A challenge sets no cooldown.
    session.request.assert_called_once()


@pytest.mark.parametrize("outer_retries", [False, True])
@pytest.mark.parametrize("native", [False, True])
async def test_accepted_door_command_with_lost_response_is_not_resent(
    monkeypatch, socket_enabled, tokens, door, outer_retries, native
):
    """A real server consumes the command and disconnects before acknowledging it."""
    from aiohttp import web

    from custom_components.myq.api.client import Client
    from custom_components.myq.api.errors import CommandUncertain

    received = []

    async def session_middleware(request, handler):
        request.headers["X-Test-Session-Middleware"] = "retained"
        try:
            return await handler(request)
        except aiohttp.ServerDisconnectedError:
            if not outer_retries:
                raise
            return await handler(request)

    async def disconnect(request):
        body = await request.read()
        received.append((request.method, body, request.headers.get("X-Test-Session-Middleware")))
        # A duplicated PUT would receive 204, exposing a hidden transport retry.
        if len(received) == 1:
            request.transport.abort()
        return web.Response(status=204)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", disconnect)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        monkeypatch.setattr("custom_components.myq.api.client.ACTIONS", base)
        if native:
            monkeypatch.setattr("custom_components.myq.api.client.NATIVE_API_BASES", {base})
        middleware_chain = (session_middleware,)
        async with aiohttp.ClientSession(middlewares=middleware_chain) as session:
            with pytest.raises(CommandUncertain, match="outcome is unknown"):
                await Client(session, tokens).command(door, "open")
            assert len(received) == 1
            assert received[0][0] == "PUT"
            assert b'"action": "open"' in received[0][1]
            assert received[0][2] == "retained"
            assert session._middlewares is middleware_chain
            assert session._retry_connection is True
            assert not session.closed

            # The same shared session's ordinary GET behavior is unaffected.
            received.clear()
            async with session.get(base + "/read") as response:
                assert response.status == 204
            assert [method for method, _, _ in received] == ["GET", "GET"]
    finally:
        await runner.cleanup()


async def test_missing_session_middleware_interface_blocks_mutations():
    session = SimpleNamespace(request=MagicMock())
    with pytest.raises(ProtocolError, match="Cannot preserve HTTP middleware"):
        await Http(session).request("PUT", "https://example.invalid/action")
    session.request.assert_not_called()

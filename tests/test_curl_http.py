"""Optional curl transport: local synthetic auth and credential-safe failures."""

import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from aiohttp import web

pytest.importorskip("curl_cffi")

from curl_cffi import CurlECode  # noqa: E402
from curl_cffi.requests.exceptions import RequestException  # noqa: E402

from custom_components.myq.api.auth import Challenge, Login  # noqa: E402
from custom_components.myq.api.client import Client  # noqa: E402
from custom_components.myq.api.const import IDENTITY  # noqa: E402
from custom_components.myq.api.curl_http import CurlHttp  # noqa: E402
from custom_components.myq.api.errors import (  # noqa: E402
    BrowserVerificationRequired,
    CannotConnect,
    ProtocolError,
    RateLimited,
)


async def test_real_curl_cookies_password_mfa_exchange_and_refresh(monkeypatch, socket_enabled):
    """All traffic stays on loopback; exercise actual libcurl cookie/form I/O."""
    received = []
    state = ""

    async def handler(request):
        nonlocal state
        received.append((request.method, request.path))
        if request.path == "/connect/authorize":
            assert request.query["acr_values"] == "unified_flow:v1  brand:myq"
            assert request.query["code_challenge_method"] == "S256"
            assert "Android" in request.headers["User-Agent"]
            assert "ha-myq" not in request.headers["User-Agent"]
            state = request.query["state"]
            response = web.Response(status=302, headers={"Location": "/signin"})
            response.set_cookie("synthetic", "cookie-pair", httponly=True)
            return response
        if request.path in {"/connect/token", "/api/v6.0/my/profile"}:
            assert not request.cookies
        else:
            assert request.cookies["synthetic"] == "cookie-pair"
        if request.path == "/signin" and request.method == "GET":
            assert "BrandId" not in request.headers
            assert "App-Version" not in request.headers
            return web.Response(
                text='<form method="post" action="/signin">'
                '<input name="csrf" type="hidden" value="csrf-pair">'
                '<input name="Email" type="email"><input name="Password" type="password">'
                '</form><script src="/Scripts/dist/sso.synthetic.js"></script>'
            )
        if request.path == "/api/v1.0/externalaccounts/IsSSOEnabledForDomain":
            assert await request.json() == {"email": "test@example.invalid"}
            assert request.headers["Sec-Fetch-Mode"] == "cors"
            assert request.headers["Sec-Fetch-Dest"] == "empty"
            assert "Sec-Fetch-User" not in request.headers
            assert "Upgrade-Insecure-Requests" not in request.headers
            assert request.headers["X-Requested-With"] == "XMLHttpRequest"
            return web.json_response(False)
        form = await request.post()
        if request.path == "/signin":
            assert dict(form) == {
                "csrf": "csrf-pair",
                "Email": "test@example.invalid",
                "Password": "synthetic-password",
            }
            assert request.headers["Origin"] == IDENTITY
            assert request.headers["Referer"] == IDENTITY + "/signin"
            assert request.headers["Sec-Fetch-Site"] == "same-origin"
            assert request.headers["Sec-Fetch-Mode"] == "navigate"
            assert request.headers["Sec-Fetch-User"] == "?1"
            return web.Response(
                text='<form method="post" action="/mfa">'
                '<input name="csrf" type="hidden" value="next-csrf">'
                '<input name="Code" autocomplete="one-time-code"></form>'
            )
        if request.path == "/mfa":
            assert dict(form) == {"csrf": "next-csrf", "Code": "123456"}
            return web.Response(
                status=302,
                headers={"Location": f"com.myqops://android?code=synthetic-code&state={state}"},
            )
        if request.path == "/connect/token":
            assert request.headers["BrandId"] == "1"
            assert request.headers["App-Version"] == "5.324.0.1132442"
            if form["grant_type"] == "authorization_code":
                assert form["code"] == "synthetic-code"
                assert len(form["code_verifier"]) == 43
                access, refresh = "synthetic-access", "synthetic-refresh"
            else:
                assert form["grant_type"] == "refresh_token"
                assert form["refresh_token"] == "synthetic-refresh"
                assert request.headers["IsRefresh"] == "true"
                access, refresh = "rotated-access", "rotated-refresh"
            return web.json_response(
                {"access_token": access, "refresh_token": refresh, "expires_in": 3600}
            )
        if request.path == "/api/v6.0/my/profile":
            assert request.headers["BrandId"] == "1"
            assert request.headers["Authorization"] == "Bearer rotated-access"
            return web.json_response({"user_id": "synthetic-user"})
        raise AssertionError("Unexpected request")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        async with CurlHttp() as http:
            for session in (http.session, http.native_session):
                original_request = session.request

                async def loopback(method, url, _request=original_request, **kwargs):
                    parsed = urlsplit(url)
                    target = base + parsed.path + ("?" + parsed.query if parsed.query else "")
                    return await _request(method, target, **kwargs)

                monkeypatch.setattr(session, "request", loopback)
            client = Client(transport=http)
            login = Login(http, client.auth, unified=True)
            assert isinstance(
                await login.start("test@example.invalid", "synthetic-password"), Challenge
            )
            tokens = await login.respond("123456")
            assert tokens.access_token == "synthetic-access"
            assert (
                await client.auth.access_token(rejected_token=tokens.access_token)
                == "rotated-access"
            )
            assert client.auth.tokens.refresh_token == "rotated-refresh"
            assert (await client.profile())["user_id"] == "synthetic-user"
        assert received == [
            ("GET", "/connect/authorize"),
            ("GET", "/signin"),
            ("POST", "/api/v1.0/externalaccounts/IsSSOEnabledForDomain"),
            ("POST", "/signin"),
            ("POST", "/mfa"),
            ("POST", "/connect/token"),
            ("POST", "/connect/token"),
            ("GET", "/api/v6.0/my/profile"),
        ]
    finally:
        await runner.cleanup()


async def test_real_curl_stops_oversized_body(monkeypatch, capsys, socket_enabled):
    monkeypatch.setattr("custom_components.myq.api.curl_http.MAX_RESPONSE", 4)
    app = web.Application()

    async def handler(request):
        return web.Response(body=b"12345678")

    app.router.add_get("/", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    try:
        async with CurlHttp() as http:
            with pytest.raises(ProtocolError, match="size limit"):
                await http.request("GET", f"http://127.0.0.1:{runner.addresses[0][1]}/")
        assert "Traceback" not in capsys.readouterr().err
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("status", [403, 429])
async def test_challenge_has_no_retry_or_cooldown(monkeypatch, status):
    async with CurlHttp() as http:
        request = AsyncMock(
            return_value=SimpleNamespace(status_code=status, headers={"cf-mitigated": "challenge"})
        )
        monkeypatch.setattr(http.session, "request", request)
        with pytest.raises(BrowserVerificationRequired):
            await http.request("GET", IDENTITY)
        assert http.last_status == status
        assert http.browser_verification_seen
        http.backoff.check(IDENTITY)  # A challenge sets no cooldown.
        request.assert_awaited_once()


async def test_rate_limit_blocks_further_requests(monkeypatch):
    async with CurlHttp() as http:
        request = AsyncMock(
            return_value=SimpleNamespace(status_code=429, headers={"retry-after": "120"})
        )
        monkeypatch.setattr(http.session, "request", request)
        with pytest.raises(RateLimited) as caught:
            await http.request("GET", IDENTITY)
        assert caught.value.retry_after == 120
        assert caught.value.server_retry_after
        with pytest.raises(RateLimited):
            await http.request("GET", IDENTITY)
        request.assert_awaited_once()


@pytest.mark.parametrize(
    "code,expected",
    [
        (CurlECode.PEER_FAILED_VERIFICATION, "TLS certificate"),
        (CurlECode.COULDNT_RESOLVE_HOST, "DNS"),
        (CurlECode.OPERATION_TIMEDOUT, "timeout"),
        (CurlECode.SSL_CONNECT_ERROR, "TLS negotiation"),
        (CurlECode.COULDNT_CONNECT, "Network"),
        (CurlECode.RECV_ERROR, "curl code"),
    ],
)
async def test_failures_hide_secrets_and_do_not_replay_posts(monkeypatch, code, expected):
    async with CurlHttp() as http:
        request = AsyncMock(side_effect=RequestException("private-secret", code=code))
        monkeypatch.setattr(http.session, "request", request)
        with pytest.raises(CannotConnect) as caught:
            await http.request("POST", IDENTITY + "/?secret=private-secret")
        assert expected in str(caught.value)
        assert "private-secret" not in str(caught.value)
        assert caught.value.__suppress_context__
        assert "RequestException: private-secret" not in "".join(
            traceback.format_exception(caught.value)
        )
        request.assert_awaited_once()
        assert request.call_args.kwargs["allow_redirects"] is False

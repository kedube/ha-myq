"""Loopback evidence for native request formatting and browser-session isolation."""

import asyncio
from contextlib import AsyncExitStack
from urllib.parse import parse_qs

import aiohttp
import pytest
from aiohttp import web

from custom_components.myq.api.client import Client
from custom_components.myq.api.const import NATIVE_HEADERS, NATIVE_USER_AGENT, PROFILES
from custom_components.myq.api.errors import CannotConnect
from custom_components.myq.api.http import Http
from custom_components.myq.api.native import encode_form


@pytest.mark.parametrize("transport_kind", ["aiohttp", "curl", "ha_android"])
async def test_native_auth_wire_profile_and_browser_isolation(
    transport_kind, monkeypatch, socket_enabled
):
    """Exercise real HTTP bytes, gzip decoding and cookie jars, without cloud access."""
    if transport_kind == "curl":
        pytest.importorskip("curl_cffi")
        from custom_components.myq.api.curl_http import CurlHttp

    received = []

    async def handler(request):
        body = await request.read()
        received.append((request.path, request.headers.copy(), body, dict(request.cookies)))
        if request.path == "/browser":
            response = web.json_response({"browser": True})
            response.set_cookie("browser_only", "kept")
            return response
        if request.path == "/connect/token":
            form = parse_qs(body.decode())
            if form["grant_type"] == ["authorization_code"]:
                access, refresh = "access-1", "refresh~* +/é"
            else:
                access, refresh = "access-2", "rotated~* +/é"
            response = web.json_response(
                {"access_token": access, "refresh_token": refresh, "expires_in": 3600}
            )
        elif request.path == "/api/v6.0/my/profile":
            response = web.json_response({"user_id": "synthetic-user"})
        elif request.path == "/connect/revocation":
            response = web.json_response({})
        else:
            return web.Response(status=404)
        response.set_cookie("native_only", "must-not-persist")
        response.enable_compression(force=web.ContentCoding.gzip)
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        monkeypatch.setattr("custom_components.myq.api.tokens.TOKEN_URL", base + "/connect/token")
        monkeypatch.setattr(
            "custom_components.myq.api.tokens.REVOCATION_URL", base + "/connect/revocation"
        )
        browser_headers = {
            "User-Agent": "SyntheticBrowser/1.0",
            "Accept": "text/html",
            "Accept-Language": "en-US",
            "Sec-Fetch-Mode": "navigate",
            "Sec-CH-UA": '"SyntheticBrowser";v="1"',
            "Upgrade-Insecure-Requests": "1",
            "Origin": base,
            "X-Requested-With": "synthetic.browser.package",
            "Referer": base + "/browser",
        }
        async with AsyncExitStack() as stack:
            session = None
            if transport_kind in {"aiohttp", "ha_android"}:
                session = await stack.enter_async_context(
                    aiohttp.ClientSession(
                        headers=browser_headers,
                        cookie_jar=aiohttp.CookieJar(unsafe=True),
                    )
                )
                if transport_kind == "ha_android":
                    client_transport = Client(session)
                    stack.push_async_callback(client_transport.close)
                    transport = client_transport.http
                else:
                    transport = Http(session)
            else:
                transport = await stack.enter_async_context(CurlHttp())

            await transport.request("GET", base + "/browser", headers=browser_headers)

            class LocalTransport:
                async def request(self, method, url, **kwargs):
                    # Route the real Client.profile call to the same local server.
                    if url.startswith(PROFILES + "/"):
                        url = base + url.removeprefix(PROFILES)
                    assert url.startswith(base + "/")
                    # Even caller-supplied browser state must not reach native OAuth/API.
                    kwargs["headers"] = {
                        **kwargs["headers"],
                        **{
                            key: value
                            for key, value in browser_headers.items()
                            if key not in {"User-Agent", "Accept"}
                        },
                        "Cookie": "injected_header=must-not-send",
                    }
                    kwargs["cookies"] = {"injected_cookie": "must-not-send"}
                    return await transport.request(method, url, **kwargs)

            client = Client(transport=LocalTransport())
            tokens = await client.auth.exchange_code(
                "code~* +/é", "v" * 43 + "~", app_check_token="synthetic-proof"
            )
            assert tokens.access_token == "access-1"
            assert await client.auth.access_token(rejected_token="access-1") == "access-2"
            assert await client.profile() == {"user_id": "synthetic-user"}
            assert await client.auth.revoke() is True
            await transport.request("GET", base + "/browser", headers=browser_headers)
            if session is not None:
                assert not session.closed
                assert session.headers["Accept"] == "text/html"
                assert session.headers["User-Agent"] == "SyntheticBrowser/1.0"
            else:
                assert not transport.native_session.cookies

        assert [row[0] for row in received] == [
            "/browser",
            "/connect/token",
            "/connect/token",
            "/api/v6.0/my/profile",
            "/connect/revocation",
            "/browser",
        ]
        for _, headers, _, cookies in received[1:-1]:
            for name, value in NATIVE_HEADERS.items():
                assert headers[name] == value
            assert headers["User-Agent"] == NATIVE_USER_AGENT == "sdk_gphone64_arm64/Android 17"
            assert "Accept" not in headers
            assert "Cookie" not in headers
            assert not cookies
            for name in browser_headers:
                if name not in {"User-Agent", "Accept"}:
                    assert name not in headers

        code, refresh, profile, revoke = received[1:-1]
        assert code[1]["Firebase-AppCheck-Token"] == "synthetic-proof"
        assert "Authorization" not in code[1]
        assert "IsRefresh" not in code[1]
        for record in (refresh, profile, revoke):
            assert "Firebase-AppCheck-Token" not in record[1]
        for record in (code, refresh, revoke):
            assert record[1]["Content-Type"] == "application/x-www-form-urlencoded"
            assert int(record[1]["Content-Length"]) == len(record[2])
        assert code[2] == (
            b"client_id=ANDROID_CGI_MYQ&scope=MyQ_Residential+offline_access"
            b"&grant_type=authorization_code&code=code%7E*+%2B%2F%C3%A9"
            b"&redirect_uri=com.myqops%3A%2F%2Fandroid&code_verifier=" + b"v" * 43 + b"%7E"
        )
        assert refresh[1]["IsRefresh"] == "true"
        assert refresh[1]["Authorization"] == "Bearer access-1"
        assert refresh[2] == (
            b"client_id=ANDROID_CGI_MYQ&grant_type=refresh_token"
            b"&refresh_token=refresh%7E*+%2B%2F%C3%A9"
        )
        assert profile[1]["Authorization"] == "Bearer access-2"
        assert revoke[1]["Authorization"] == "Bearer access-2"
        assert revoke[2] == (
            b"client_id=ANDROID_CGI_MYQ&token_type_hint=refresh_token"
            b"&token=rotated%7E*+%2B%2F%C3%A9"
        )
        for record in (received[0], received[-1]):
            for name, value in browser_headers.items():
                assert record[1][name] == value
            assert "BrandId" not in record[1]
            assert "App-Version" not in record[1]
        assert received[0][3] == {}
        assert received[-1][3] == {"browser_only": "kept"}
    finally:
        await runner.cleanup()


def test_okhttp_form_escapes_keys_and_percent_encoded_looking_values():
    assert encode_form({"a~* +é": "%2F /~*"}) == b"a%7E*+%2B%C3%A9=%252F+%2F%7E*"


async def test_concurrent_unload_and_shutdown_close_pools_once():
    """HA can dispose an entry and shut down its coordinator concurrently."""
    async with aiohttp.ClientSession() as session:
        client = Client(session)
        # The test fixture blocks C-level networking before any handle is sent.
        with pytest.raises(CannotConnect):
            await client.http.request("GET", PROFILES, headers=NATIVE_HEADERS)
        await asyncio.gather(client.close(), client.close())
        assert not session.closed
        with pytest.raises(CannotConnect, match="transport is closed"):
            await client.http.request("GET", PROFILES, headers=NATIVE_HEADERS)

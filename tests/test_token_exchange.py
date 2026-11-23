"""App Check handoff and safe, specific token-endpoint rejection diagnostics."""

import asyncio
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest

from custom_components.myq.api.client import Client
from custom_components.myq.api.const import IDENTITY, NATIVE_HEADERS, PROFILES
from custom_components.myq.api.errors import (
    ApplicationVerificationError,
    AuthenticationError,
    CannotConnect,
    ProtocolError,
    RequestUnauthorized,
    SessionRenewalFailed,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.native import FORM_CONTENT_TYPE
from custom_components.myq.api.oauth import OAuthSession
from custom_components.myq.api.tokens import (
    REVOCATION_URL,
    TokenManager,
    Tokens,
    exchange_diagnostics,
)
from custom_components.myq.config_flow import error_key

from .conftest import json_response


async def test_app_check_is_used_only_for_code_exchange_and_is_not_persisted(http):
    http.request.side_effect = [
        json_response({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}),
        json_response({"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}),
    ]
    manager = TokenManager(http)
    oauth = OAuthSession()
    callback = f"com.myqops://android?code=code&state={oauth.state}"
    await oauth.complete(callback, manager, app_check_token="private-app-check-token")
    call = http.request.call_args
    assert call.args == ("POST", IDENTITY + "/connect/token")
    assert call.kwargs["headers"] == {
        **NATIVE_HEADERS,
        "Content-Type": FORM_CONTENT_TYPE,
        "Firebase-AppCheck-Token": "private-app-check-token",
    }
    assert parse_qs(call.kwargs["data"].decode())["code_verifier"] == [oauth.verifier]
    assert manager.last_exchange["app_check_supplied"] is True
    assert "private-app-check-token" not in json.dumps(manager.last_exchange)
    assert "private-app-check-token" not in json.dumps(manager.tokens.as_dict())
    assert not hasattr(manager, "app_check_token")
    await manager.access_token(rejected_token="access", force=True)
    # ft0/i0 also adds the stored access token to the refresh request.
    assert http.request.call_args.kwargs["headers"] == {
        **NATIVE_HEADERS,
        "Content-Type": FORM_CONTENT_TYPE,
        "IsRefresh": "true",
        "Authorization": "Bearer access",
    }
    assert manager.last_exchange["app_check_supplied"] is False


@pytest.mark.parametrize("status", [401, 403])
async def test_application_rejection_has_specific_error_and_never_replays_code(http, status):
    http.request.return_value = json_response(
        {"code": "401.122", "message": "private-secret", "description": "private-secret"}, status
    )
    manager = TokenManager(http)
    oauth = OAuthSession()
    callback = f"com.myqops://android?code=private-code&state={oauth.state}"
    with pytest.raises(ApplicationVerificationError) as caught:
        await oauth.complete(callback, manager)
    assert error_key(caught.value) == "app_verification"
    assert "private-" not in str(caught.value)
    assert manager.last_exchange == {
        "http_status": status,
        "response_format": "json",
        "myq_code": "401.122",
        "oauth_error": None,
        "app_check_supplied": False,
    }
    with pytest.raises(AuthenticationError, match="already been used"):
        await oauth.complete(callback, manager)
    http.request.assert_awaited_once()
    assert manager.tokens is None


@pytest.mark.parametrize("code", ["invalid_grant", "invalid_client"])
async def test_standard_oauth_errors_remain_distinct(http, code):
    http.request.return_value = json_response(
        {"error": code, "error_description": "private-secret"}, 400
    )
    manager = TokenManager(http)
    with pytest.raises(AuthenticationError) as caught:
        await manager.exchange_code("private-code", "private-verifier")
    assert code in str(caught.value)
    assert manager.last_exchange["oauth_error"] == code
    assert "private-" not in json.dumps(manager.last_exchange)
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize(
    "data",
    [
        {"error": "private-secret", "code": "private-secret"},
        {"error": {"private": "secret"}, "code": ["secret"]},
        {
            "error": "invalid_grant",
            "description": "private-secret",
            "access_token": "private-secret",
        },
    ],
)
def test_only_allowlisted_server_fields_are_reported(data):
    summary = exchange_diagnostics(json_response(data, 401))
    assert "private" not in json.dumps(summary)
    assert summary["response_format"] == "json"
    assert summary["myq_code"] is None


@pytest.mark.parametrize(
    "body,content_type,expected",
    [
        (b"private-secret", "text/plain", "other"),
        (b"private-secret", "text/html", "html"),
        (b"", "", "empty"),
    ],
)
def test_non_json_rejections_do_not_expose_bodies(body, content_type, expected):
    summary = exchange_diagnostics(Response(401, {"content-type": content_type}, body))
    assert summary["response_format"] == expected
    assert "private" not in json.dumps(summary)


@pytest.mark.parametrize(
    "token", ["", "two words", "line\r\ninjection", "é", "null\0byte", "x" * 16385]
)
async def test_malformed_app_check_value_is_not_sent(http, token):
    with pytest.raises(ProtocolError):
        await TokenManager(http).exchange_code("code", "verifier", app_check_token=token)
    http.request.assert_not_awaited()


async def test_metadata_is_scoped_to_native_requests_and_never_carries_proof(http, tokens):
    client = Client(transport=http, tokens=tokens)
    http.request.return_value = json_response({})
    await client.request("GET", PROFILES, "/api/v6.0/my/profile")
    assert http.request.call_args.kwargs["headers"] == {
        **NATIVE_HEADERS,
        "Authorization": "Bearer " + tokens.access_token,
    }
    await client.request("GET", "https://media.hubs.tend-us.tendplatform.com", "/read")
    assert http.request.call_args.kwargs["headers"] == {
        "Authorization": "Bearer " + tokens.access_token,
    }


def _held_token_response(body):
    """A token endpoint that has received the POST but not yet answered."""
    sent, release = asyncio.Event(), asyncio.Event()

    async def request(method, url, **kwargs):
        sent.set()  # The server has now consumed the code or refresh token.
        await release.wait()
        return json_response(body)

    http = AsyncMock()
    http.request.side_effect = request
    return http, sent, release


async def _cancel_after_send(operation, sent, release):
    caller = asyncio.create_task(operation)
    await sent.wait()
    caller.cancel()  # e.g. HA's camera-image timeout, a status pause, a closed offer
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)


async def test_cancelled_caller_does_not_discard_refresh_rotation():
    http, sent, release = _held_token_response(
        {"access_token": "new-access", "refresh_token": "rotated", "expires_in": 3600}
    )
    saved: list[Tokens] = []
    on_update = AsyncMock(side_effect=saved.append)
    manager = TokenManager(http, Tokens("old", "old-refresh", 0), on_update)
    await _cancel_after_send(manager.access_token(), sent, release)
    assert manager.tokens.refresh_token == "rotated"
    assert saved and saved[-1].refresh_token == "rotated"
    # The lock was held until the rotation landed, so later callers reuse it.
    assert await manager.access_token() == "new-access"
    http.request.assert_awaited_once()


async def test_cancelled_code_exchange_still_publishes_tokens():
    http, sent, release = _held_token_response(
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
    )
    manager = TokenManager(http)
    await _cancel_after_send(manager.exchange_code("one-time-code", "verifier"), sent, release)
    assert manager.tokens is not None and manager.tokens.refresh_token == "refresh"


# --- Session renewal follows the app's sign-out rules (ip0/j.f91078u, ft0/g1, ft0/c1) ----


@pytest.mark.parametrize("body", [{"error": "invalid_grant"}, {"code": "401.102"}, {"code": "400"}])
@pytest.mark.parametrize("status", [400, 401])
async def test_refresh_ends_the_session_only_for_the_apps_sign_out_answers(http, status, body):
    http.request.return_value = json_response({**body, "error_description": "private"}, status)
    manager = TokenManager(http, Tokens("old", "old-refresh", 0))
    with pytest.raises(AuthenticationError, match="ended this session") as caught:
        await manager.access_token()
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "status,body",
    [
        (400, {"error": "invalid_request"}),
        (400, {"error": "invalid_client"}),
        (400, {"error": "temporarily_unavailable"}),
        (401, None),
        (401, {"code": "401.999"}),
    ],
)
async def test_other_refresh_refusals_keep_the_session_for_a_later_try(http, status, body):
    http.request.return_value = (
        json_response(body, status) if body is not None else Response(status, {}, b"")
    )
    manager = TokenManager(http, Tokens("old", "old-refresh", 0))
    with pytest.raises(SessionRenewalFailed):
        await manager.access_token()
    assert manager.tokens.refresh_token == "old-refresh"
    assert error_key(SessionRenewalFailed()) == "invalid_auth"  # Only inside setup.


async def test_refresh_outage_is_a_connection_error(http):
    http.request.return_value = Response(503, {}, b"")
    with pytest.raises(CannotConnect):
        await TokenManager(http, Tokens("old", "old-refresh", 0)).access_token()


async def test_rotation_without_expiry_is_kept_with_the_apps_default(http):
    http.request.return_value = json_response({"access_token": "a2", "refresh_token": "r2"})
    saved = AsyncMock()
    manager = TokenManager(http, Tokens("old", "old-refresh", 0), saved)
    assert await manager.access_token() == "a2"
    assert manager.tokens.refresh_token == "r2" and manager.tokens.lifetime == 3600
    saved.assert_awaited_once_with(manager.tokens)


@pytest.mark.parametrize("lifetime,margin", [(3600, 180), (7200, 300), (600, 30), (0, 60)])
def test_renewal_point_follows_the_apps_effective_lifetime(lifetime, margin):
    # ip0/j.z0: max(95% of the lifetime, lifetime - 5 minutes); 0 = saved without a lifetime.
    tokens = Tokens("a", "r", 10_000.0, lifetime)
    assert tokens.renew_at == pytest.approx(10_000.0 - margin)


async def test_token_renews_at_the_effective_lifetime(http, monkeypatch):
    http.request.return_value = json_response(
        {"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
    )
    manager = TokenManager(http, Tokens("current", "r1", 10_000.0, 3600))
    monkeypatch.setattr("custom_components.myq.api.tokens.time.time", lambda: 9_819.0)
    assert await manager.access_token() == "current"
    monkeypatch.setattr("custom_components.myq.api.tokens.time.time", lambda: 9_820.0)
    assert await manager.access_token() == "new"


def test_saved_sessions_from_earlier_versions_still_load():
    tokens = Tokens(**{"access_token": "a", "refresh_token": "r", "expires_at": 5.0})
    assert tokens.lifetime == 0 and tokens.renew_at == -55.0
    assert Tokens(**tokens.as_dict()) == tokens


# --- Sign-out revocation (gp0/f.d -> ip0/k.N) ---------------------------------------------


async def test_revoke_sends_the_apps_sign_out_request(http, tokens):
    http.request.return_value = Response(200, {}, b"")
    manager = TokenManager(http, tokens)
    assert await manager.revoke() is True
    call = http.request.call_args
    assert call.args == ("POST", REVOCATION_URL)
    assert parse_qs(call.kwargs["data"].decode()) == {
        "client_id": ["ANDROID_CGI_MYQ"],
        "token_type_hint": ["refresh_token"],
        "token": ["test-refresh"],
    }
    assert call.kwargs["headers"] == {
        **NATIVE_HEADERS,
        "Content-Type": FORM_CONTENT_TYPE,
        "Authorization": "Bearer test-access",
    }
    assert manager.tokens is None
    http.request.assert_awaited_once()


async def test_revoke_renews_an_expired_session_first_and_revokes_the_new_token(http):
    http.request.side_effect = [
        json_response({"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}),
        Response(200, {}, b""),
    ]
    manager = TokenManager(http, Tokens("old", "r1", 0))
    assert await manager.revoke() is True
    revoke = http.request.call_args
    assert parse_qs(revoke.kwargs["data"].decode())["token"] == ["r2"]
    assert revoke.kwargs["headers"]["Authorization"] == "Bearer a2"


async def test_revoke_without_renewal_still_sends_the_refresh_token(http):
    http.request.side_effect = [
        json_response({"error": "invalid_grant"}, 400),
        Response(400, {}, b""),
    ]
    manager = TokenManager(http, Tokens("old", "r1", 0))
    assert await manager.revoke() is False
    assert "Authorization" not in http.request.call_args.kwargs["headers"]


async def test_waiters_share_one_failed_renewal(http):
    """Eight requests queued behind a renewal that fails do not each send their own."""
    http.request.return_value = json_response({"error": "server_error"}, 503)
    manager = TokenManager(http, Tokens("a", "r", time.time() - 1, 3600.0))
    results = await asyncio.gather(
        *(manager.access_token() for _ in range(8)), return_exceptions=True
    )
    assert all(isinstance(result, CannotConnect) for result in results)
    assert http.request.await_count == 1


async def test_failed_renewal_is_tried_again_after_a_pause(http, monkeypatch):
    http.request.return_value = json_response({"error": "server_error"}, 503)
    manager = TokenManager(http, Tokens("a", "r", time.time() - 1, 3600.0))
    with pytest.raises(CannotConnect):
        await manager.access_token()
    monkeypatch.setattr("custom_components.myq.api.tokens.RENEWAL_RETRY_SECONDS", 0)
    http.request.return_value = json_response(
        {"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}
    )
    assert await manager.access_token() == "new"
    assert http.request.await_count == 2


async def test_ended_session_is_never_asked_again_with_the_same_refresh_token(http, monkeypatch):
    monkeypatch.setattr("custom_components.myq.api.tokens.RENEWAL_RETRY_SECONDS", 0)
    http.request.return_value = json_response({"error": "invalid_grant"}, 400)
    manager = TokenManager(http, Tokens("a", "r", time.time() - 1, 3600.0))
    for _ in range(3):
        with pytest.raises(AuthenticationError, match="ended this session"):
            await manager.access_token()
    assert http.request.await_count == 1
    # A new sign-in replaces the tokens, and renewal works again.
    manager.tokens = Tokens("b", "r2", time.time() - 1, 3600.0)
    http.request.return_value = json_response(
        {"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}
    )
    assert await manager.access_token() == "new"


async def test_endpoint_that_always_answers_401_does_not_rotate_the_session_every_poll(http):
    rotations = 0

    async def request(method, url, **kwargs):
        nonlocal rotations
        if url.startswith(IDENTITY):
            rotations += 1
            return json_response(
                {
                    "access_token": f"a{rotations}",
                    "refresh_token": f"r{rotations}",
                    "expires_in": 3600,
                }
            )
        return Response(401, {}, b"")

    http.request.side_effect = request
    saved = []

    async def save(tokens):
        saved.append(tokens.refresh_token)

    client = Client(transport=http, tokens=Tokens("a0", "r0", time.time() + 3600, 3600.0))
    client.auth.on_update = save
    for _ in range(5):
        with pytest.raises(RequestUnauthorized):
            await client.request("GET", PROFILES, "/api/v6.0/optional")
    # One renewal answers the first 401. MyQ only just issued that token, so the
    # following 401s are about the request and nothing is rotated or saved again.
    assert rotations == 1 and saved == ["r1"]
    # An explicit session check still renews.
    assert await client.auth.access_token(rejected_token="a1", force=True) == "a2"


async def test_renewal_outage_after_a_401_is_not_an_unknown_command_outcome(http, door):
    """The command was answered 401, so MyQ did not act on it."""
    http.request.side_effect = [Response(401, {}, b""), CannotConnect("identity down")]
    client = Client(transport=http, tokens=Tokens("a", "r", time.time() + 3600, 3600.0))
    with pytest.raises(RequestUnauthorized):
        await client.command(door, "open")


async def test_close_waits_for_a_renewal_already_sent(http):
    """The old refresh token is spent once sent; only the answer carries the new one."""
    saved = []
    answer = asyncio.Event()

    async def request(method, url, **kwargs):
        await answer.wait()
        return json_response({"access_token": "new", "refresh_token": "new-r", "expires_in": 3600})

    async def save(tokens):
        saved.append(tokens.refresh_token)

    http.request.side_effect = request
    client = Client(transport=http, tokens=Tokens("old", "old-r", time.time() - 1, 3600.0))
    client.auth.on_update = save
    caller = asyncio.create_task(client.auth.access_token())
    await asyncio.sleep(0.01)
    caller.cancel()  # Unload cancels whatever was waiting for the token.
    await asyncio.gather(caller, return_exceptions=True)
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0.01)
    assert not closing.done()
    answer.set()
    await closing
    assert saved == ["new-r"]


def test_numeric_and_oversized_token_answer_fields_are_read_like_api_answers():
    assert exchange_diagnostics(json_response({"code": 400}, 400))["myq_code"] == "400"
    # A session-ended code sent as a number still ends the session.
    with pytest.raises(AuthenticationError):
        TokenManager._reject("refresh", None, "400")
    huge = b'{"access_token":"n","refresh_token":"nr","expires_in":1' + b"0" * 400 + b"}"
    assert Tokens.from_response(json.loads(huge)).lifetime == 3600.0


def test_deeply_nested_json_is_a_protocol_error():
    with pytest.raises(ProtocolError, match="invalid JSON"):
        Response(200, {}, b"[" * 100_000).json()

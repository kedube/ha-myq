"""Synthetic password/MFA transcripts; no claim of live MFA verification."""

import base64
import hashlib
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.myq.api.auth import Challenge, Login
from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import (
    AccessDenied,
    AuthenticationError,
    ProtocolError,
    UnsupportedAuthStep,
)
from custom_components.myq.api.http import Response

PASSWORD_PAGE = b"""<form method="post" action="/Account/LoginWithEmail">
<input type="hidden" name="__RequestVerificationToken" value="csrf-cookie-pair">
<input type="hidden" name="ReturnUrl" value="/connect/authorize/callback">
<input type="email" name="Email"><input type="password" name="Password"></form>"""
MFA_PAGE = b"""<form method="post" action="/synthetic-mfa">
<input type="hidden" name="__RequestVerificationToken" value="new-csrf">
<input name="Code" autocomplete="one-time-code"></form>"""


def callback(login, state=None):
    return Response(
        302,
        {
            "Location": "com.myqops://android?code=one-time-code&state="
            + (state or login.oauth.state)
        },
        b"",
    )


async def test_password_and_mfa_preserve_form_csrf_and_pkce(http, tokens):
    manager = AsyncMock()
    manager.exchange_code.return_value = tokens
    login = Login(http, manager)
    query = parse_qs(urlsplit(login.authorize_url).query)
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        Response(200, {}, MFA_PAGE),
        callback(login),
    ]
    result = await login.start("test@example.invalid", "not-a-real-password")
    assert isinstance(result, Challenge) and result.kind == "code"
    posted = http.request.call_args.kwargs["data"]
    assert posted["__RequestVerificationToken"] == "csrf-cookie-pair"
    assert posted["Email"] == "test@example.invalid"
    assert posted["Password"] == "not-a-real-password"
    assert http.request.call_args.kwargs["headers"]["Origin"] == IDENTITY
    assert http.request.call_args.kwargs["headers"]["Referer"].startswith(IDENTITY + "/")
    assert await login.respond("123456") == tokens
    assert http.request.call_args.kwargs["data"] == {
        "Code": "123456",
        "__RequestVerificationToken": "new-csrf",
    }
    manager.exchange_code.assert_awaited_once()
    assert manager.exchange_code.call_args.args == ("one-time-code", login.oauth.verifier)
    assert callable(manager.exchange_code.call_args.kwargs["before_send"])
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(login.oauth.verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    ]
    assert not hasattr(login, "password")


async def test_select_mfa_method_then_code(http, tokens):
    manager = AsyncMock()
    manager.exchange_code.return_value = tokens
    login = Login(http, manager)
    page = b"""<form method="post" action="/synthetic-method"><select name="Method">
    <option value="sms">SMS</option><option value="email">Email</option></select></form>"""
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        Response(200, {}, page),
        Response(200, {}, MFA_PAGE),
        callback(login),
    ]
    result = await login.start("user", "password")
    assert result.choices == {"sms": "SMS", "email": "Email"}
    with pytest.raises(AuthenticationError):
        await login.respond("unadvertised-method")
    assert (await login.respond("sms")).kind == "code"
    assert await login.respond("123456") == tokens


async def test_redirect_state_mismatch_does_not_exchange_code(http):
    manager = AsyncMock()
    login = Login(http, manager)
    http.request.side_effect = [Response(200, {}, PASSWORD_PAGE), callback(login, "wrong-state")]
    with pytest.raises(AuthenticationError):
        await login.start("user", "password")
    manager.exchange_code.assert_not_awaited()


@pytest.mark.parametrize(
    "destination",
    [
        "https://untrusted.invalid/login",
        "http://partner-identity.myq-cloud.com/login",
        "https://partner-identity.myq-cloud.com.evil.invalid/",
    ],
)
async def test_credentials_never_follow_untrusted_form_action(http, destination):
    login = Login(http, AsyncMock())
    http.request.return_value = Response(
        200,
        {},
        PASSWORD_PAGE.replace(
            b"/Account/LoginWithEmail",
            destination.encode(),
        ),
    )
    with pytest.raises(ProtocolError):
        await login.start("user", "private-password")
    assert http.request.await_count == 1


async def test_unsupported_captcha_or_javascript_is_not_fake_mfa(http):
    login = Login(http, AsyncMock())
    http.request.return_value = Response(200, {}, b"<script>challenge()</script>")
    with pytest.raises(UnsupportedAuthStep):
        await login.start("user", "password")


async def test_password_rejected_is_not_retried(http):
    login = Login(http, AsyncMock())
    http.request.return_value = Response(200, {}, PASSWORD_PAGE)
    with pytest.raises(AuthenticationError):
        await login.start("user", "password")
    assert http.request.await_count == 2


async def test_app_verification_failure_is_visible(http):
    manager = AsyncMock()
    manager.exchange_code.side_effect = AccessDenied("App verification required")
    login = Login(http, manager)
    http.request.side_effect = [Response(200, {}, PASSWORD_PAGE), callback(login)]
    with pytest.raises(AccessDenied):
        await login.start("user", "password")


async def test_expired_mfa_session_does_not_send_code(http):
    login = Login(http, AsyncMock())
    http.request.side_effect = [Response(200, {}, PASSWORD_PAGE), Response(200, {}, MFA_PAGE)]
    await login.start("user", "password")
    login.oauth.expires = 0
    with pytest.raises(AuthenticationError):
        await login.respond("123456")
    assert http.request.await_count == 2


async def test_307_does_not_replay_password(http):
    login = Login(http, AsyncMock())
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        Response(307, {"Location": IDENTITY + "/again"}, b""),
    ]
    with pytest.raises(UnsupportedAuthStep):
        await login.start("user", "password")
    assert http.request.await_count == 2


async def test_auth_progress_and_form_diagnostics_exclude_values(http):
    messages = []
    login = Login(http, AsyncMock(), messages.append)
    page = b"""<form method="post" action="/mfa?secret=private-query">
    <input name="Code" autocomplete="one-time-code" value="private-code">
    <input name="csrf" type="hidden" value="private-csrf"></form>"""
    http.request.side_effect = [Response(200, {}, PASSWORD_PAGE), Response(200, {}, page)]
    await login.start("private-email", "private-password")
    output = str(messages) + str(login.form_structure)
    assert "private-" not in output
    assert "one-time-code" in output
    assert login.form_structure[0]["hidden_field_count"] == 1


# --- The app's WebView error checks (ip0/j.B0 errorId, ip0/j.A0 #validation-errors) ----


async def test_error_id_in_a_sign_in_redirect_is_a_failed_sign_in(http):
    login = Login(http, AsyncMock())
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        Response(302, {"Location": "/Account/Error?errorId=private-id"}, b""),
    ]
    with pytest.raises(AuthenticationError) as caught:
        await login.start("test@example.invalid", "not-a-real-password")
    assert "private" not in str(caught.value)
    assert http.request.await_count == 2  # The error page itself is never fetched.


async def test_validation_error_box_is_a_failed_sign_in(http):
    page = b"""<div id="validation-errors"><span>  Private server wording  </span></div>
    <p>Something went wrong</p>"""
    login = Login(http, AsyncMock())
    http.request.side_effect = [Response(200, {}, PASSWORD_PAGE), Response(200, {}, page)]
    with pytest.raises(AuthenticationError) as caught:
        await login.start("test@example.invalid", "not-a-real-password")
    assert "Private" not in str(caught.value)


async def test_empty_validation_box_does_not_fail_and_wrong_code_can_be_retyped(http, tokens):
    manager = AsyncMock()
    manager.exchange_code.return_value = tokens
    login = Login(http, manager)
    empty = b'<div id="validation-errors"></div>' + PASSWORD_PAGE
    retry = b'<div id="validation-errors">Invalid code</div>' + MFA_PAGE
    http.request.side_effect = [
        Response(200, {}, empty),
        Response(200, {}, MFA_PAGE),
        Response(200, {}, retry),
    ]
    assert (await login.start("test@example.invalid", "pw")).kind == "code"
    # A shown error with the code form still present is a retry, not a failure.
    assert (await login.respond("000000")).kind == "code"


async def test_form_sign_in_sends_the_requested_language(http):
    login = Login(http, AsyncMock(), locale="de-DE")
    assert parse_qs(urlsplit(login.authorize_url).query)["ui_locales"] == ["de-DE"]

"""Fresh synthetic transcripts for unified sign-in behavior and failure boundaries."""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from custom_components.myq.api.auth import FormParser, Login
from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import (
    AuthenticationError,
    CannotConnect,
    ProtocolError,
    UnsupportedAuthStep,
)
from custom_components.myq.api.http import Response

from .test_auth import MFA_PAGE, PASSWORD_PAGE, callback


def html(body):
    return Response(200, {}, body.encode())


def redirect(path):
    return Response(302, {"Location": path}, b"")


def consent(destination="/connect/authorize/callback?transaction=synthetic"):
    action = "/Consent?returnUrl=" + quote(destination, safe="")
    return html(
        f'<form method="post" action="{action}">'
        '<input name="csrf" type="hidden" value="consent-proof">'
        '<input name="scope" type="checkbox" value="residential" checked>'
        '<input name="scope" type="checkbox" value="offline" checked>'
        '<input name="scope" type="checkbox" value="unselected">'
        '<button name="button" value="no">Cancel</button>'
        '<button name="button" value="yes">Continue</button></form>'
    )


def verification(method, csrf):
    return html(
        '<form method="post" action="/AccountMfa/VerifyOtp">'
        f'<input name="SelectedMfaMethod" type="hidden" value="{method}">'
        f'<input name="csrf" type="hidden" value="{csrf}">'
        '<input name="AccountEmail" type="email" readonly value="masked">'
        '<input name="Digits" type="tel" autocomplete="one-time-code"></form>'
    )


@pytest.mark.parametrize("acknowledge_with_page", [False, True])
async def test_unified_email_retry_consent_and_code_exchange(http, tokens, acknowledge_with_page):
    manager = AsyncMock()
    manager.exchange_code.return_value = tokens
    login = Login(http, manager)
    first = html(
        '<form method="post"><input type="email" name="AccountEmail">'
        '<input type="password" name="Passphrase">'
        '<input type="hidden" name="csrf" value="sign-in-proof">'
        '<input name="Remember" type="checkbox" checked value="true">'
        '<input name="Remember" type="hidden" value="false"></form>'
        '<script src="/Scripts/dist/sso.synthetic.js"></script>'
    )
    retry = verification("Email", "retry-proof")
    retry.body = b'<span class="field-validation-error">private rejection</span>' + retry.body
    responses = [
        first,
        redirect("/AccountMfa/VerifyOtp?returnUrl=keep&selectedMfaMethod=Sms"),
        verification("Sms", "sms-proof"),
        verification("Email", "email-proof"),
        retry,
        redirect("/Consent"),
        consent(),
    ]
    if acknowledge_with_page:
        responses.append(html("<p>Consent recorded</p>"))
    responses.append(callback(login))
    http.request.side_effect = responses

    challenge = await login.start("person@example.invalid", "synthetic-password")
    assert challenge.kind == "code" and challenge.field_name == "Digits"
    assert login.oauth.unified
    calls = http.request.call_args_list
    assert dict(calls[1].kwargs["data"])["Passphrase"] == "synthetic-password"
    assert [(n, v) for n, v in calls[1].kwargs["data"] if n == "Remember"] == [
        ("Remember", "true"),
        ("Remember", "false"),
    ]
    assert parse_qs(urlsplit(calls[3].args[1]).query) == {
        "returnUrl": ["keep"],
        "selectedMfaMethod": ["Email"],
    }
    assert not any("IsSSOEnabledForDomain" in c.args[1] for c in calls)
    assert (await login.respond("000001")).kind == "code"
    assert await login.respond("000002") == tokens
    calls = http.request.call_args_list
    assert calls[5].kwargs["data"]["csrf"] == "retry-proof"
    assert calls[5].kwargs["data"]["Digits"] == "000002"
    posted = calls[7].kwargs["data"]
    assert posted == [
        ("csrf", "consent-proof"),
        ("scope", "residential"),
        ("scope", "offline"),
        ("button", "yes"),
    ]
    if acknowledge_with_page:
        assert calls[-1].args == (
            "GET",
            IDENTITY + "/connect/authorize/callback?transaction=synthetic",
        )
    manager.exchange_code.assert_awaited_once()
    assert manager.exchange_code.call_args.args[1] == login.oauth.verifier
    assert login.challenge is None


@pytest.mark.parametrize(
    "destination",
    [
        "https://outside.invalid/connect/authorize/callback",
        "//outside.invalid/connect/authorize/callback",
        "http://partner-identity.myq-cloud.com/connect/authorize/callback",
        "/Home/Secure",
        "com.myqops://android?code=injected",
        "/connect/authorize/callback#bad",
    ],
)
async def test_consent_continuation_cannot_escape_authorization(http, destination):
    manager = AsyncMock()
    login = Login(http, manager)
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        Response(200, {}, MFA_PAGE),
        redirect("/Consent"),
        consent(destination),
        html("Recorded"),
    ]
    await login.start("user", "password")
    with pytest.raises(ProtocolError):
        await login.respond("654321")
    assert http.request.await_count == 5
    manager.exchange_code.assert_not_awaited()


async def test_lost_consent_response_cannot_repost_otp_or_consent(http):
    login = Login(http, AsyncMock())
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        Response(200, {}, MFA_PAGE),
        redirect("/Consent"),
        consent(),
        CannotConnect("lost acknowledgement"),
    ]
    await login.start("user", "password")
    with pytest.raises(CannotConnect):
        await login.respond("654321")
    with pytest.raises(AuthenticationError, match="No pending MFA"):
        await login.respond("654321")
    assert http.request.await_count == 5


@pytest.mark.parametrize(
    "control,expected",
    [
        ('<input name="PinValue" id="enteredOtp" type="number">', "PinValue"),
        ('<input name="EntryCode"><input name="Email" type="email">', "EntryCode"),
        ('<input name="Digits" type="tel">', "Digits"),
        ('<input name="Digits"><input name="Another">', None),
        ('<input name="Otp" disabled><input name="Only" type="email">', None),
        ('<input name="Code" readonly><input name="Otp" type="number">', "Otp"),
        ('<input name="Otp"><input name="Code" autocomplete="one-time-code">', None),
    ],
)
def test_otp_metadata_and_verify_endpoint_fallback(control, expected):
    parser = FormParser()
    parser.feed('<form method="post" action="/AccountMfa/VerifyOtp">' + control + "</form>")
    assert parser.forms[0].identify("code", IDENTITY) == expected


async def test_multiple_credential_forms_are_rejected_without_sending_password(http):
    login = Login(http, AsyncMock())
    http.request.return_value = Response(200, {}, PASSWORD_PAGE + PASSWORD_PAGE)
    with pytest.raises(UnsupportedAuthStep):
        await login.start("user", "private-password")
    http.request.assert_awaited_once()


@pytest.mark.parametrize("css", ["validation-summary-errors", "field-validation-error"])
async def test_server_validation_text_is_not_exposed(http, css):
    login = Login(http, AsyncMock())
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        html(f'<section class="{css}"><p>private account information</p></section>'),
    ]
    with pytest.raises(AuthenticationError) as error:
        await login.start("user", "private-password")
    assert "private" not in str(error.value)

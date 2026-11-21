"""Wire structure from the user-supplied successful website-login HAR.

Only route/field facts are reproduced. Every form value, email, password, OTP,
CSRF value, and page body below is synthetic. The capture has no OAuth exchange.
"""

import json
from unittest.mock import AsyncMock

import pytest

from custom_components.myq.api.auth import Challenge, FormParser, Login
from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import ProtocolError, UnsupportedAuthStep
from custom_components.myq.api.http import Response

RETURN_QUERY = "?returnUrl=%2FHome%2FSecure"
LOGIN_PATH = "/Account/LoginWithEmail" + RETURN_QUERY
MFA_PATH = "/AccountMfa/VerifyOtp" + RETURN_QUERY
PASSWORD_PAGE = (
    '<form method="post" action="' + LOGIN_PATH + '">'
    '<input name="ReturnUrl" type="hidden" value="/Home/Secure">'
    '<input name="Email" type="email" autocomplete="false">'
    '<input name="Password" type="password" autocomplete="false">'
    '<input name="__RequestVerificationToken" type="hidden" value="synthetic-password-csrf">'
    '</form><script src="/Scripts/dist/sso.synthetic.js"></script>'
).encode()
MFA_PAGE = (
    '<form method="post" action="' + MFA_PATH + '">'
    '<input name="SelectedMfaMethod" type="hidden" value="Email">'
    '<input name="IsSmsMethodEnabled" type="hidden" value="False">'
    '<input name="MaskedEmail" type="hidden" value="synthetic-masked-email">'
    '<input name="MaskedPhoneNumber" type="hidden" value="synthetic-masked-phone">'
    '<input name="Otp" type="number" pattern="[0-9]*" value="">'
    '<input name="__Invariant" type="hidden" value="Otp">'
    '<input type="submit" disabled>'
    '<input name="__RequestVerificationToken" type="hidden" value="synthetic-mfa-csrf">'
    "</form>"
).encode()


async def test_captured_website_sequence_and_email_mfa_are_not_api_authentication(http):
    manager = AsyncMock()
    login = Login(http, manager)
    http.request.side_effect = [
        Response(200, {}, b"<html>synthetic public home</html>"),
        Response(302, {"Location": IDENTITY + "/Account/Login?ReturnUrl=%2FHome%2FSecure"}, b""),
        Response(302, {"Location": "/Account/LoginSelect" + RETURN_QUERY}, b""),
        Response(302, {"Location": LOGIN_PATH}, b""),
        Response(200, {}, PASSWORD_PAGE),
        Response(200, {}, b"false"),
        Response(302, {"Location": MFA_PATH}, b""),
        Response(200, {}, MFA_PAGE),
        Response(302, {"Location": "/Home/Secure"}, b""),
        Response(200, {}, b"<html>synthetic website session</html>"),
    ]
    await login._navigate("GET", IDENTITY + "/")
    forms = await login._navigate("GET", IDENTITY + "/Home/Secure")
    assert login._has_sso_lookup
    await login._check_sso("synthetic@example.invalid")
    result = login._result(
        await login._submit(
            forms[0], login._page_url, {"Email": "synthetic@example.invalid", "Password": "fake"}
        )
    )
    assert isinstance(result, Challenge)
    assert result.kind == "code" and result.field_name == "Otp"
    assert result.page_url == IDENTITY + MFA_PATH
    with pytest.raises(UnsupportedAuthStep, match="website landing page.*no API session"):
        await login.respond("001234")
    calls = http.request.call_args_list
    assert [(c.args[0], c.args[1].removeprefix(IDENTITY)) for c in calls] == [
        ("GET", "/"),
        ("GET", "/Home/Secure"),
        ("GET", "/Account/Login?ReturnUrl=%2FHome%2FSecure"),
        ("GET", "/Account/LoginSelect" + RETURN_QUERY),
        ("GET", LOGIN_PATH),
        ("POST", "/api/v1.0/externalaccounts/IsSSOEnabledForDomain"),
        ("POST", LOGIN_PATH),
        ("GET", MFA_PATH),
        ("POST", MFA_PATH),
        ("GET", "/Home/Secure"),
    ]
    assert calls[5].kwargs["json"] == {"email": "synthetic@example.invalid"}
    assert calls[6].kwargs["data"]["ReturnUrl"] == "/Home/Secure"
    assert calls[8].kwargs["data"] == {
        "SelectedMfaMethod": "Email",
        "IsSmsMethodEnabled": "False",
        "MaskedEmail": "synthetic-masked-email",
        "MaskedPhoneNumber": "synthetic-masked-phone",
        "Otp": "001234",
        "__Invariant": "Otp",
        "__RequestVerificationToken": "synthetic-mfa-csrf",
    }
    assert calls[8].kwargs["headers"]["Origin"] == IDENTITY
    assert calls[8].kwargs["headers"]["Sec-Fetch-Site"] == "same-origin"
    assert calls[9].kwargs["headers"]["Referer"] == IDENTITY + MFA_PATH
    manager.exchange_code.assert_not_awaited()


@pytest.mark.parametrize("reply", [True, None, {"enabled": False}])
async def test_sso_lookup_rejects_unsupported_or_malformed_results_before_password(http, reply):
    login = Login(http, AsyncMock())
    http.request.side_effect = [
        Response(200, {}, PASSWORD_PAGE),
        Response(200, {}, json.dumps(reply).encode()),
    ]
    with pytest.raises(UnsupportedAuthStep if reply is True else ProtocolError):
        await login.start("synthetic@example.invalid", "synthetic-password")
    assert http.request.await_count == 2
    assert "synthetic-password" not in str(http.request.call_args_list)


@pytest.mark.parametrize(
    "script,expected",
    [
        ("/Scripts/dist/sso.synthetic.js?v=1", True),
        (IDENTITY + "/Scripts/dist/sso.synthetic.js", True),
        ("https://untrusted.invalid/Scripts/dist/sso.synthetic.js", False),
        ("/Scripts/dist/other.js", False),
    ],
)
def test_sso_lookup_only_enabled_by_the_identity_page_bundle(script, expected):
    parser = FormParser()
    parser.feed(f'<script src="{script}"></script>')
    assert parser.has_sso_lookup is expected

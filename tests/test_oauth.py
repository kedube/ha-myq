"""APK-derived sign-in parameters and browser-independent OAuth boundaries."""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.myq.api.errors import AuthenticationError, ProtocolError
from custom_components.myq.api.oauth import OAuthSession, find_callback, ui_locale
from custom_components.myq.api.tokens import TokenManager

from .conftest import json_response


@pytest.mark.parametrize(
    "unified,acr",
    [
        (True, "unified_flow:v1  brand:myq"),
        (False, "  brand:myq"),
    ],
)
def test_signin_parameters_follow_app_call_chain(unified, acr):
    oauth = OAuthSession(unified=unified)
    query = parse_qs(urlsplit(oauth.authorize_url).query)
    assert query["acr_values"] == [acr]
    assert query["client_id"] == ["ANDROID_CGI_MYQ"]
    assert query["redirect_uri"] == ["com.myqops://android"]
    assert len(oauth.verifier) == 43  # 32 random bytes, URL-safe base64 without padding.


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid?code=test&state={state}",
        "com.myqops://android.evil.invalid?code=test&state={state}",
        "com.myqops://android/wrong?code=test&state={state}",
        "com.myqops://android?code=test&state={state}#fragment",
        "com.myqops://android?code=first&code=second&state={state}",
    ],
)
async def test_invalid_callback_never_exchanges_code(url):
    oauth = OAuthSession()
    manager = AsyncMock()
    with pytest.raises(ProtocolError):
        await oauth.complete(url.format(state=oauth.state), manager)
    manager.exchange_code.assert_not_awaited()


async def test_state_mismatch_never_exchanges_code():
    manager = AsyncMock()
    with pytest.raises(AuthenticationError):
        await OAuthSession().complete("com.myqops://android?code=test&state=wrong", manager)
    manager.exchange_code.assert_not_awaited()


async def test_authorization_code_is_never_replayed(http):
    oauth = OAuthSession()
    manager = TokenManager(http)
    http.request.return_value = json_response(
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
    )
    url = f"com.myqops://android?code=test&state={oauth.state}"
    assert (await oauth.complete(url, manager)).access_token == "access"
    with pytest.raises(AuthenticationError):
        await oauth.complete(url, manager)
    http.request.assert_awaited_once()


async def test_expired_browser_session_cannot_exchange():
    oauth = OAuthSession()
    oauth.expires = 0
    manager = AsyncMock()
    with pytest.raises(AuthenticationError):
        await oauth.complete(f"com.myqops://android?code=test&state={oauth.state}", manager)
    manager.exchange_code.assert_not_awaited()


@pytest.mark.parametrize(
    "language,country,expected",
    [
        ("en", "US", "en-US"),
        ("fr", "ca", "fr-CA"),
        ("en-GB", "US", "en-GB"),
        ("pt_BR", None, "pt-BR"),
        ("de", None, "de"),
        (None, "US", "en-US"),
        ("not a language", "US", "en-US"),
        ("en", "USA", "en"),
    ],
)
def test_ui_locale_matches_the_apps_language_country_tag(language, country, expected):
    assert ui_locale(language, country) == expected


def test_authorize_url_carries_the_locale_and_refuses_an_unsafe_one():
    query = parse_qs(urlsplit(OAuthSession(locale="fr-CA").authorize_url).query)
    assert query["ui_locales"] == ["fr-CA"]
    query = parse_qs(urlsplit(OAuthSession(locale="x&prompt=none").authorize_url).query)
    assert query["ui_locales"] == ["en-US"] and query["prompt"] == ["login"]


CALLBACK = "com.myqops://android?code=abc&scope=openid&state=s-1&iss=https%3A%2F%2Fpartner"


@pytest.mark.parametrize(
    "pasted",
    [
        CALLBACK,
        f"  {CALLBACK}\n",
        f'"{CALLBACK}"',
        f"Failed to launch '{CALLBACK}' because the scheme does not have a registered handler.",
        f"Failed to launch '{CALLBACK}' because …\nFailed to launch '{CALLBACK}' because …",
    ],
    ids=["bare", "spaces", "quoted", "console", "repeated"],
)
def test_find_callback_takes_the_address_from_pasted_text(pasted):
    assert find_callback(pasted) == CALLBACK


@pytest.mark.parametrize(
    "pasted",
    ["", "https://partner-identity.myq-cloud.com/", f"{CALLBACK} {CALLBACK}x", "x" * 16385],
    ids=["empty", "other-address", "two-addresses", "too-long"],
)
def test_find_callback_refuses_text_without_one_address(pasted):
    with pytest.raises(ProtocolError):
        find_callback(pasted)

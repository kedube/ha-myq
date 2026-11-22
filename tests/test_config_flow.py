"""Direct credential setup, reauthentication, discovery retries and polling options."""

import json
import os
from dataclasses import asdict
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import (
    ApplicationVerificationError,
    AuthenticationError,
    BrowserVerificationRequired,
    CannotConnect,
)
from custom_components.myq.api.http import Response
from custom_components.myq.const import CONF_ENABLE_CAMERAS, CONF_POLL_INTERVAL, DOMAIN

from .conftest import json_response
from .test_auth import MFA_PAGE, PASSWORD_PAGE
from .test_firebase import firebase_token

PROFILE = {"user_id": "verified-user", "email": "test@example.invalid"}
CREDENTIALS = {"username": "test@example.invalid", "password": "private-password"}


@pytest.fixture(autouse=True)
def allow_custom(enable_custom_integrations, app_check_registration_file):
    yield


@pytest.fixture
def cloud():
    with (
        patch(
            "custom_components.myq.api.client.Client.profile",
            AsyncMock(return_value=PROFILE),
        ),
        patch(
            "custom_components.myq.api.client.Client.discover", AsyncMock(return_value={})
        ) as discover,
        patch("custom_components.myq.async_setup_entry", AsyncMock(return_value=True)),
    ):
        yield discover


async def test_connect_opens_only_email_and_password_without_network(hass):
    with patch("custom_components.myq.api.http.Http.request", new_callable=AsyncMock) as http:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form" and result["step_id"] == "user"
    assert "menu_options" not in result
    assert set(result["data_schema"].schema) == {"username", "password"}
    http.assert_not_awaited()


async def test_reauth_opens_credentials_and_prefills_the_existing_email(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="verified-user", data={"username": "saved@example.invalid"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] == "form" and result["step_id"] == "reauth_confirm"
    assert "menu_options" not in result
    assert set(result["data_schema"].schema) == {"username", "password"}
    assert result["data_schema"]({"password": "private"})["username"] == "saved@example.invalid"


@pytest.mark.parametrize(
    "removed_field", ["next_step_id", "handoff", "refresh_token", "app_check_token"]
)
async def test_removed_setup_inputs_cannot_select_another_route(hass, removed_field):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, removed_field: "removed"}
        )


async def _login_with_tokens(hass, tokens, discover_error):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    calls = []

    async def start(login, username, password):
        calls.append((username, password))
        login.token_manager.tokens = tokens
        return tokens

    with (
        patch("custom_components.myq.api.auth.Login.start", start),
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(side_effect=discover_error),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert calls == [(CREDENTIALS["username"], CREDENTIALS["password"])]
    return result


async def test_form_finish_retries_discovery_without_signing_in_again(hass, cloud, tokens):
    result = await _login_with_tokens(hass, tokens, CannotConnect("lost"))
    assert result["step_id"] == "finish"
    assert result["errors"] == {"base": "cannot_connect"}
    with patch("custom_components.myq.api.auth.Login.start", new_callable=AsyncMock) as start:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    start.assert_not_awaited()
    assert result["type"] == "create_entry"
    assert result["data"]["tokens"] == tokens.as_dict()
    assert set(result["data"]) == {"username", "tokens"}
    assert "private-password" not in str(result["data"])


async def test_form_finish_aborts_when_the_session_is_rejected(hass, cloud, tokens):
    result = await _login_with_tokens(hass, tokens, CannotConnect("lost"))
    cloud.side_effect = AuthenticationError("revoked")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "abort" and result["reason"] == "invalid_auth"


@pytest.mark.parametrize(
    "error,key",
    [
        (ApplicationVerificationError(), "app_verification"),
        (BrowserVerificationRequired(), "browser_verification"),
        (AuthenticationError("rejected"), "invalid_auth"),
    ],
)
async def test_sign_in_errors_return_to_credentials_and_create_nothing(hass, error, key):
    with patch("custom_components.myq.api.auth.Login.start", AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": key}
    assert set(result["data_schema"].schema) == {"username", "password"}
    assert "private-password" not in str(result)
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_mfa_exchange_rejection_returns_to_credentials_without_replaying(hass, registration):
    states, exchanges = [], []
    proof = firebase_token(registration)

    async def request(_self, method, url, **kwargs):
        if url.startswith("https://firebaseappcheck.googleapis.com/"):
            assert kwargs["json"] == {"debugToken": registration.debug_token}
            return json_response({"token": proof, "ttl": "3600s"})
        if method == "GET":
            states.append(parse_qs(urlsplit(url).query)["state"][0])
            return Response(200, {}, PASSWORD_PAGE)
        if url == IDENTITY + "/connect/token":
            exchanges.append(kwargs)
            return json_response({"code": "401.122"}, 401)
        if "Password" in kwargs["data"]:
            return Response(200, {}, MFA_PAGE)
        return Response(
            302, {"Location": f"com.myqops://android?code=one-shot&state={states[-1]}"}, b""
        )

    with (
        patch("custom_components.myq.api.http.Http.request", request),
        patch("custom_components.myq.api.android_http.AndroidHttp.request", request),
        patch(
            "custom_components.myq.api.firebase.FirebaseRegistration.read",
            return_value=registration,
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "mfa"
        assert set(result["data_schema"].schema) == {"response"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "app_verification"}
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        assert flow._login is None and flow._session is None
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "mfa"
    assert len(exchanges) == 1
    assert len(states) == 2 and states[0] != states[1]
    assert exchanges[0]["headers"]["Firebase-AppCheck-Token"] == proof
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_default_form_gets_firebase_proof_and_persists_only_session(
    hass, cloud, registration
):
    state, calls = "", []
    proof = firebase_token(registration)

    async def request(_self, method, url, **kwargs):
        nonlocal state
        calls.append(urlsplit(url).netloc)
        if method == "GET":
            if urlsplit(url).path == "/Consent":
                return Response(
                    200,
                    {},
                    b'<form method="post" action="/Consent?returnUrl='
                    b'%2Fconnect%2Fauthorize%2Fcallback">'
                    b'<input name="csrf" type="hidden" value="consent-csrf">'
                    b'<button name="button" value="yes">Continue</button></form>',
                )
            if urlsplit(url).path == "/connect/authorize/callback":
                return Response(
                    302, {"Location": f"com.myqops://android?code=once&state={state}"}, b""
                )
            state = parse_qs(urlsplit(url).query)["state"][0]
            assert "unified_flow:v1" in parse_qs(urlsplit(url).query)["acr_values"][0]
            return Response(200, {}, PASSWORD_PAGE)
        if "firebaseappcheck.googleapis.com" in url:
            assert kwargs["headers"]["X-Android-Package"] == registration.package
            return json_response({"token": proof, "ttl": "3600s"})
        if url.endswith("/connect/token"):
            assert kwargs["headers"]["Firebase-AppCheck-Token"] == proof
            return json_response(
                {
                    "access_token": "session-access",
                    "refresh_token": "session-refresh",
                    "expires_in": 3600,
                }
            )
        if urlsplit(url).path == "/Consent":
            assert kwargs["data"] == {"csrf": "consent-csrf", "button": "yes"}
            return Response(200, {}, b"<p>Consent completed</p>")
        if "Password" in kwargs["data"]:
            return Response(200, {}, MFA_PAGE)
        return Response(302, {"Location": "/Consent"}, b"")

    with (
        patch("custom_components.myq.api.http.Http.request", request),
        patch("custom_components.myq.api.android_http.AndroidHttp.request", request),
        patch(
            "custom_components.myq.api.firebase.FirebaseRegistration.read",
            return_value=registration,
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "mfa"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
    assert result["type"] == "create_entry"
    assert set(result["data"]) == {"username", "tokens"}
    assert result["data"]["tokens"]["access_token"] == "session-access"
    assert proof not in str(result["data"])
    assert registration.debug_token not in str(result["data"])
    assert calls[-2:] == ["firebaseappcheck.googleapis.com", "partner-identity.myq-cloud.com"]


@pytest.mark.parametrize(
    "failure,error_key",
    [
        ("missing", "app_check_not_configured"),
        ("unreadable", "app_check_config_unreadable"),
        ("malformed", "app_check_config_invalid"),
    ],
)
async def test_registration_preflight_stops_before_sign_in(
    hass, app_check_registration_file, failure, error_key
):
    if failure == "missing":
        app_check_registration_file.unlink()
    elif failure == "malformed":
        app_check_registration_file.write_text('{"debug_token":"private-debug"}')
    real_open = os.open

    def open_file(path, *args, **kwargs):
        if failure == "unreadable" and path == app_check_registration_file:
            raise PermissionError("private-path")
        return real_open(path, *args, **kwargs)

    with (
        patch("custom_components.myq.api.firebase.os.open", side_effect=open_file),
        patch("custom_components.myq.api.auth.Login.start", new_callable=AsyncMock) as start,
        patch("custom_components.myq.api.http.Http.request", new_callable=AsyncMock) as firebase,
        patch(
            "custom_components.myq.api.android_http.AndroidHttp.request", new_callable=AsyncMock
        ) as myq,
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error_key}
    assert "private-" not in str(result)
    start.assert_not_awaited()
    firebase.assert_not_awaited()
    myq.assert_not_awaited()
    assert not hass.config_entries.async_entries(DOMAIN)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._login is None and flow._session is None and flow._client is None


async def test_registration_can_be_installed_then_same_flow_retried(
    hass, app_check_registration_file, registration
):
    app_check_registration_file.unlink()
    with patch("custom_components.myq.api.auth.Login.start", new_callable=AsyncMock) as start:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["errors"] == {"base": "app_check_not_configured"}
        start.assert_not_awaited()
    app_check_registration_file.write_text(json.dumps(asdict(registration)))
    requests = []

    async def request(_self, method, url, **kwargs):
        requests.append((method, url))
        return Response(200, {}, PASSWORD_PAGE if method == "GET" else MFA_PAGE)

    with (
        patch("custom_components.myq.api.android_http.AndroidHttp.request", request),
        patch("custom_components.myq.api.http.Http.request", new_callable=AsyncMock) as firebase,
    ):
        retry = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert retry["flow_id"] == result["flow_id"]
    assert retry["step_id"] == "mfa"
    assert [method for method, _ in requests] == ["GET", "POST"]
    firebase.assert_not_awaited()
    hass.config_entries.flow.async_abort(retry["flow_id"])
    await hass.async_block_till_done()


async def test_form_authorization_uses_home_assistant_language(hass):
    hass.config.language, hass.config.country = "fr", "CA"
    urls = []

    async def start(login, username, password):
        urls.append(login.authorize_url)
        raise AuthenticationError("stop before network")

    with patch("custom_components.myq.api.auth.Login.start", start):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert parse_qs(urlsplit(urls[0]).query)["ui_locales"] == ["fr-CA"]


async def test_options_save_and_bounds(hass, tokens):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u", data={"tokens": tokens.as_dict()})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 10, CONF_ENABLE_CAMERAS: True}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 120, CONF_ENABLE_CAMERAS: False}
    )
    assert result["type"] == "create_entry"
    assert entry.options == {
        CONF_POLL_INTERVAL: 120,
        CONF_ENABLE_CAMERAS: False,
        "panel_enabled": False,
        "panel_title": "MyQ",
        "panel_icon": "mdi:garage-variant",
        "panel_require_admin": False,
    }

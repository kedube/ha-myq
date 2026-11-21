"""Direct credential setup, reauthentication, discovery retries and polling options."""

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

PROFILE = {"user_id": "verified-user", "email": "test@example.invalid"}
CREDENTIALS = {"username": "test@example.invalid", "password": "private-password"}


@pytest.fixture(autouse=True)
def allow_custom(enable_custom_integrations):
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


async def test_mfa_exchange_rejection_returns_to_credentials_without_replaying(hass):
    states, exchanges = [], []

    async def request(_self, method, url, **kwargs):
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

    with patch("custom_components.myq.api.http.Http.request", request):
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
    assert "Firebase-AppCheck-Token" not in exchanges[0]["headers"]
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])
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
    assert entry.options == {CONF_POLL_INTERVAL: 120, CONF_ENABLE_CAMERAS: False}

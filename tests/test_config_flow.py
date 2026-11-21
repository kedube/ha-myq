"""Token import, browser retry and options steps of the config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import (
    AuthenticationError,
    CannotConnect,
    RateLimited,
)
from custom_components.myq.const import CONF_ENABLE_CAMERAS, CONF_POLL_INTERVAL, DOMAIN

from .conftest import app_check_jwt, json_response

PROFILE = {"user_id": "verified-user", "email": "test@example.invalid"}


@pytest.fixture(autouse=True)
def allow_custom(enable_custom_integrations):
    yield


@pytest.fixture
def cloud():
    """Profile/discovery succeed; entry setup itself is out of scope here."""
    with (
        patch(
            "custom_components.myq.api.client.Client.profile",
            AsyncMock(return_value=PROFILE),
        ),
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={}),
        ) as discover,
        patch("custom_components.myq.async_setup_entry", AsyncMock(return_value=True)),
    ):
        yield discover


async def _menu(hass, step):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": step})


async def test_refresh_token_import_renews_and_saves_rotated_tokens(hass, cloud):
    rotated = {"access_token": "new-access", "refresh_token": "rotated", "expires_in": 3600}
    with patch(
        "custom_components.myq.api.http.Http.request",
        AsyncMock(return_value=json_response(rotated)),
    ) as request:
        result = await _menu(hass, "token")
        assert result["step_id"] == "token"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"refresh_token": "  imported-refresh \n"}
        )
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == "verified-user"
    assert result["data"]["tokens"]["refresh_token"] == "rotated"
    sent = request.call_args
    assert sent.args == ("POST", IDENTITY + "/connect/token")
    assert sent.kwargs["data"]["refresh_token"] == "imported-refresh"  # Whitespace removed.


async def test_rejected_refresh_token_shows_error_and_creates_nothing(hass, cloud):
    rejected = json_response({"error": "invalid_grant"}, 400)
    with patch(
        "custom_components.myq.api.http.Http.request",
        AsyncMock(return_value=rejected),
    ):
        result = await _menu(hass, "token")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"refresh_token": "revoked"}
        )
    assert result["type"] == "form" and result["errors"] == {"base": "invalid_auth"}
    assert not hass.config_entries.async_entries(DOMAIN)
    cloud.assert_not_awaited()


async def test_token_step_honors_the_rate_limit_cooldown(hass, cloud):
    with patch(
        "custom_components.myq.api.http.Http.request",
        AsyncMock(side_effect=RateLimited(120)),
    ) as request:
        result = await _menu(hass, "token")
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"refresh_token": "any"}
            )
            assert result["errors"] == {"base": "rate_limited"}
    request.assert_awaited_once()  # The second attempt never reached the network.


async def _browser_with_tokens(hass, tokens, discover_error):
    """Complete OAuth once, then fail discovery so only the finish step can be retried."""
    result = await _menu(hass, "browser")
    flow = hass.config_entries.flow._progress[result["flow_id"]]

    async def complete(callback_url, auth, **kwargs):
        auth.tokens = tokens

    with (
        patch(
            "custom_components.myq.config_flow.read_result",
            MagicMock(return_value=("com.myqops://android?code=x", app_check_jwt())),
        ),
        patch.object(flow._oauth, "callback_code", MagicMock()),
        patch.object(flow._oauth, "complete", AsyncMock(side_effect=complete)) as exchange,
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(side_effect=discover_error),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"handoff": "synthetic"}
        )
    exchange.assert_awaited_once()
    return result


async def test_browser_finish_retries_discovery_without_replaying_the_code(hass, cloud, tokens):
    result = await _browser_with_tokens(hass, tokens, CannotConnect("lost"))
    assert result["step_id"] == "finish"
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"]["tokens"] == tokens.as_dict()


async def test_browser_finish_aborts_when_the_session_is_rejected(hass, cloud, tokens):
    result = await _browser_with_tokens(hass, tokens, CannotConnect("lost"))
    cloud.side_effect = AuthenticationError("revoked")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "abort" and result["reason"] == "invalid_auth"


async def test_token_import_keeps_the_rotated_session_when_discovery_fails(hass, cloud):
    # The pasted token was already exchanged; asking for it again would fail.
    rotated = {"access_token": "new-access", "refresh_token": "rotated", "expires_in": 3600}
    with patch(
        "custom_components.myq.api.http.Http.request",
        AsyncMock(return_value=json_response(rotated)),
    ) as request:
        cloud.side_effect = [CannotConnect("lost"), {}]
        result = await _menu(hass, "token")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"refresh_token": "imported"}
        )
        assert result["step_id"] == "finish"
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"]["tokens"]["refresh_token"] == "rotated"
    request.assert_awaited_once()


async def test_refused_renewal_during_import_is_reported_as_invalid(hass, cloud):
    with patch(
        "custom_components.myq.api.http.Http.request",
        AsyncMock(return_value=json_response({"error": "invalid_request"}, 400)),
    ):
        result = await _menu(hass, "token")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"refresh_token": "imported"}
        )
    assert result["step_id"] == "token" and result["errors"] == {"base": "invalid_auth"}


LINK = "https://www.myliftmaster.com/ids/auth/onetime-login?token=one-time-secret&x=1"


async def test_one_time_link_signs_in_like_the_app(hass, cloud):
    issued = {"access_token": "access", "refresh_token": "refresh", "expires_in": 1800}
    with patch(
        "custom_components.myq.api.http.Http.request",
        AsyncMock(return_value=json_response(issued)),
    ) as request:
        result = await _menu(hass, "onetime")
        assert result["step_id"] == "onetime"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"link": f"  {LINK}\n"}
        )
    assert result["type"] == "create_entry"
    assert result["data"]["tokens"]["refresh_token"] == "refresh"
    assert result["data"]["tokens"]["lifetime"] == 1800
    sent = request.call_args
    assert sent.args == ("POST", IDENTITY + "/api/v1.0/auth/token")
    assert sent.kwargs["data"] == {"client_id": "ANDROID_CGI_MYQ"}
    assert sent.kwargs["headers"]["Authorization"] == "Bearer one-time-secret"
    assert "Firebase-AppCheck-Token" not in sent.kwargs["headers"]
    request.assert_awaited_once()


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/ids/auth/onetime-login?token=x",
        "http://www.myliftmaster.com/ids/auth/onetime-login?token=x",
        "https://www.myliftmaster.com/other?token=x",
        "https://www.myliftmaster.com/ids/auth/onetime-login",
        "https://www.myliftmaster.com/ids/auth/onetime-login?token=a&token=b",
        "two words",
    ],
)
async def test_unusable_one_time_link_is_refused_before_any_request(hass, cloud, link):
    with patch("custom_components.myq.api.http.Http.request", AsyncMock()) as request:
        result = await _menu(hass, "onetime")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"link": link})
    assert result["errors"] == {"base": "invalid_link"}
    request.assert_not_awaited()


async def test_used_one_time_link_is_rejected_and_never_resent(hass, cloud):
    with patch(
        "custom_components.myq.api.http.Http.request",
        AsyncMock(return_value=json_response({"code": "401"}, 401)),
    ) as request:
        result = await _menu(hass, "onetime")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"link": LINK})
    assert result["step_id"] == "onetime" and result["errors"] == {"base": "invalid_auth"}
    request.assert_awaited_once()


async def test_browser_request_uses_the_home_assistant_language(hass, cloud):
    hass.config.language, hass.config.country = "fr", "CA"
    result = await _menu(hass, "browser")
    assert "ui_locales=fr-CA" in result["description_placeholders"]["request"]


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

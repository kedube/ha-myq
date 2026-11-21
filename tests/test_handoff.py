"""HA-owned PKCE transactions and private browser/helper handoffs."""

import importlib.util
import json
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.app_check import AppCheckProof
from custom_components.myq.api.const import NATIVE_HEADERS
from custom_components.myq.api.errors import (
    AuthenticationError,
    CannotConnect,
    ProtocolError,
)
from custom_components.myq.api.handoff import (
    BrowserAuthorization,
    create_request,
    create_result,
    read_result,
)
from custom_components.myq.api.oauth import OAuthSession
from custom_components.myq.const import DOMAIN

from .conftest import app_check_jwt, json_response


def result_for(request, *, token=None, state=None):
    browser = BrowserAuthorization(request)
    callback = f"com.myqops://android?code=private-code&state={state or browser.state}"
    return json.dumps(
        {"version": 1, "callback_url": callback, "app_check_token": token or app_check_jwt()}
    )


def test_public_request_does_not_share_verifier_and_result_checks_issuer():
    oauth = OAuthSession()
    public = create_request(oauth)
    assert oauth.verifier not in public
    browser = BrowserAuthorization(public)
    assert not hasattr(browser, "verifier")
    assert browser.authorize_url == oauth.authorize_url
    callback = f"com.myqops://android?code=private-code&state={oauth.state}"
    proof = AppCheckProof.from_token(app_check_jwt())
    assert read_result(create_result(browser, callback, proof)) == (callback, proof.token)
    with pytest.raises(AuthenticationError):
        browser.callback_code(callback + "&iss=https://evil.invalid")


@pytest.mark.parametrize(
    "before,after",
    [
        ("partner-identity.myq-cloud.com", "evil.invalid"),
        ("ANDROID_CGI_MYQ", "OTHER_CLIENT"),
        ("S256", "plain"),
        ("com.myqops%3A%2F%2Fandroid", "https%3A%2F%2Fevil.invalid"),
        ("response_type=code", "response_type=code&response_type=token"),
        ("ui_locales=en-US", "ui_locales=en-US&ui_locales=fr-CA"),
        ("ui_locales=en-US", "ui_locales=%3Cscript%3E"),
    ],
)
def test_helper_refuses_modified_authorization_requests(before, after):
    request = create_request(OAuthSession())
    assert before in request
    with pytest.raises(ProtocolError):
        BrowserAuthorization(request.replace(before, after))


@pytest.mark.parametrize("value", ["{", "[]", '{"version":true}', "x" * 32769])
def test_invalid_handoff_is_bounded_and_redacted(value):
    with pytest.raises(ProtocolError, match="Invalid browser sign-in handoff"):
        read_result(value)


def test_expired_public_request_is_rejected():
    request = json.loads(create_request(OAuthSession()))
    request["expires_at"] = time.time() - 1
    with pytest.raises(AuthenticationError):
        BrowserAuthorization(json.dumps(request))


def test_helper_output_is_private_and_cannot_overwrite(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/browser_handoff.py"
    spec = importlib.util.spec_from_file_location("browser_handoff_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "result.token.json"
    module.write_private_result(path, "private-result")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        module.write_private_result(path, "replacement")
    assert path.read_text() == "private-result\n"


@pytest.fixture
def enable_ha(enable_custom_integrations):
    yield


async def start_browser(hass, *, entry=None):
    if entry is None:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    else:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
        )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "browser"}
    )


@pytest.fixture
def cloud():
    with (
        patch("custom_components.myq.api.http.Http.request", new_callable=AsyncMock) as http,
        patch(
            "custom_components.myq.api.client.Client.profile",
            AsyncMock(return_value={"user_id": "user", "email": "user@example.invalid"}),
        ) as profile,
        patch("custom_components.myq.api.client.Client.discover", AsyncMock(return_value={})),
        patch("custom_components.myq.async_setup_entry", AsyncMock(return_value=True)),
    ):
        http.return_value = json_response(
            {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
        )
        yield http, profile


async def test_ha_browser_exchanges_once_and_saves_only_session(hass, enable_ha, cloud):
    http, _ = cloud
    result = await start_browser(hass)
    public = result["description_placeholders"]["request"]
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    verifier = flow._oauth.verifier
    session = flow._session
    token = app_check_jwt()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"handoff": result_for(public, token=token)}
    )
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == "user"
    assert set(result["data"]) == {"username", "tokens"}
    for secret in (token, verifier, "private-code"):
        assert secret not in str(result)
    assert session.closed
    assert flow._oauth is None
    http.assert_awaited_once()
    assert http.call_args.kwargs["headers"] == {**NATIVE_HEADERS, "Firebase-AppCheck-Token": token}
    assert http.call_args.kwargs["data"]["code_verifier"] == verifier
    await hass.async_block_till_done()


async def test_ha_local_proof_failure_keeps_transaction_and_hides_input(hass, enable_ha, cloud):
    http, _ = cloud
    result = await start_browser(hass)
    public = result["description_placeholders"]["request"]
    token = app_check_jwt(time.time() - 1)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"handoff": result_for(public, token=token)}
    )
    assert result["errors"] == {"base": "app_check_expired"}
    assert token not in str(result)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert not flow._oauth.used
    assert BrowserAuthorization(public).state == flow._oauth.state
    http.assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"handoff": result_for(public)}
    )
    assert result["type"] == "create_entry"
    http.assert_awaited_once()
    await hass.async_block_till_done()


async def test_ha_rejected_exchange_starts_fresh_transaction_and_cannot_replay(
    hass, enable_ha, cloud
):
    http, _ = cloud
    http.return_value = json_response({"code": "401.122"}, 401)
    result = await start_browser(hass)
    old = result["description_placeholders"]["request"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"handoff": result_for(old)}
    )
    assert result["errors"] == {"base": "app_verification"}
    assert BrowserAuthorization(result["description_placeholders"]["request"]).state != (
        BrowserAuthorization(old).state
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"handoff": result_for(old)}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    http.assert_awaited_once()
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_ha_discovery_retry_does_not_repeat_exchange(hass, enable_ha, cloud):
    http, profile = cloud
    profile.side_effect = [CannotConnect("Unavailable"), {"user_id": "user"}]
    result = await start_browser(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"handoff": result_for(result["description_placeholders"]["request"])},
    )
    assert result["step_id"] == "finish"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    http.assert_awaited_once()
    await hass.async_block_till_done()


async def test_browser_reauth_rejects_a_different_account(hass, enable_ha, cloud):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="different-user", data={"username": "old"})
    entry.add_to_hass(hass)
    result = await start_browser(hass, entry=entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"handoff": result_for(result["description_placeholders"]["request"])},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data == {"username": "old"}
    await hass.async_block_till_done()


def test_helper_accepts_the_home_assistant_language():
    browser = BrowserAuthorization(create_request(OAuthSession(locale="pt-BR")))
    assert "ui_locales=pt-BR" in browser.authorize_url

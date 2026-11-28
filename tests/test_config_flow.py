"""Direct credential setup, reauthentication, discovery retries and polling options."""

import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers.selector import TextSelector

from custom_components.myq.api.app_check import AppCheckProof
from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import (
    ApplicationVerificationError,
    AuthenticationError,
    CannotConnect,
    RateLimited,
)
from custom_components.myq.api.firebase import MAX_REGISTRATION_BYTES, FirebaseRegistration
from custom_components.myq.api.http import Response
from custom_components.myq.const import CONF_ENABLE_CAMERAS, CONF_POLL_INTERVAL, DOMAIN

from .conftest import SIGN_IN_CHOICES, choose, json_response, start_sign_in
from .ha import MockConfigEntry
from .test_auth import MFA_PAGE, PASSWORD_PAGE
from .test_firebase import firebase_token

PROFILE = {"user_id": "verified-user", "email": "test@example.invalid"}
CREDENTIALS = {"username": "test@example.invalid", "password": "private-password"}


@pytest.fixture(autouse=True)
def allow_custom(enable_custom_integrations, app_check_registration_file):
    yield


@pytest.fixture
def cloud(firebase_exchange):
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


def assert_masked(selector):
    assert isinstance(selector, TextSelector)
    assert selector.config["type"] == "password"


async def test_connect_offers_password_or_browser_sign_in_without_network(hass):
    with patch("custom_components.myq.api.http.Http.request", new_callable=AsyncMock) as http:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] == "menu" and result["step_id"] == "user"
        assert result["menu_options"] == SIGN_IN_CHOICES
        password = await choose(hass, result, "password")
        assert password["type"] == "form" and password["step_id"] == "password"
        assert list(password["data_schema"].schema) == ["username", "password", "debug_token"]
        assert_masked(password["data_schema"].schema["debug_token"])
        assert "debug_token" not in password["data_schema"](CREDENTIALS)
        hass.config_entries.flow.async_abort(password["flow_id"])
        browser = await start_sign_in(hass, "browser_start")
        assert browser["type"] == "form" and browser["step_id"] == "browser_start"
        assert list(browser["data_schema"].schema) == ["debug_token"]
        assert_masked(browser["data_schema"].schema["debug_token"])
        assert browser["data_schema"]({}) == {}
        hass.config_entries.flow.async_abort(browser["flow_id"])
    http.assert_not_awaited()


async def test_reauth_offers_both_and_prefills_the_existing_email(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="verified-user", data={"username": "saved@example.invalid"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] == "menu" and result["step_id"] == "reauth_confirm"
    result = await choose(hass, result, "password")
    assert result["step_id"] == "password"
    assert list(result["data_schema"].schema) == ["username", "password", "debug_token"]
    assert result["data_schema"]({"password": "private"})["username"] == "saved@example.invalid"
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "removed_field", ["next_step_id", "handoff", "refresh_token", "app_check_token"]
)
async def test_removed_setup_inputs_cannot_select_another_route(hass, removed_field):
    result = await start_sign_in(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, removed_field: "removed"}
        )


async def _login_with_tokens(hass, tokens, discover_error):
    result = await start_sign_in(hass)
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
        (AuthenticationError("rejected"), "invalid_auth"),
    ],
)
async def test_sign_in_errors_return_to_credentials_and_create_nothing(
    hass, error, key, firebase_exchange
):
    with patch("custom_components.myq.api.auth.Login.start", AsyncMock(side_effect=error)):
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "password"
    assert result["errors"] == {"base": key}
    assert list(result["data_schema"].schema) == ["username", "password", "debug_token"]
    assert "private-password" not in str(result)
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("proof_expires", [False, True])
async def test_mfa_exchange_rejection_returns_to_credentials_without_replaying(
    hass, registration, proof_expires
):
    states, exchanges = [], []
    proof_requests = []
    proof = firebase_token(registration)

    async def request(_self, method, url, **kwargs):
        if url.startswith("https://firebaseappcheck.googleapis.com/"):
            proof_requests.append(url)
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
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "mfa"
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        if proof_expires:
            flow._login.app_check_provider._proof = AppCheckProof(proof, time.time() + 200)
        assert set(result["data_schema"].schema) == {"response"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
        assert result["step_id"] == "password"
        assert result["errors"] == {"base": "app_verification"}
        assert len(proof_requests) == (2 if proof_expires else 1)
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


@pytest.mark.parametrize("status", [401, 403])
async def test_revoked_debug_token_stops_before_hosted_login(hass, status):
    with (
        patch("custom_components.myq.api.auth.Login.start", new_callable=AsyncMock) as start,
        patch(
            "custom_components.myq.api.http.Http.request",
            AsyncMock(return_value=json_response({"error": "private-debug"}, status)),
        ) as firebase,
    ):
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["errors"] == {"base": "app_check_integrity"}
    assert "private-debug" not in str(result)
    start.assert_not_awaited()
    firebase.assert_awaited_once()
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._client is None and flow._login is None
    # The refused registration left no proof; ending the flow releases its provider.
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert flow._provider is None and flow._proof_session is None


def mfa_cloud(registration, *, code_form, firebase=None):
    """Offline hosted login with MFA: `code_form` answers each verification code post."""
    proof = firebase_token(registration)
    seen = {"states": [], "codes": [], "proofs": [], "exchanges": []}

    async def request(_self, method, url, **kwargs):
        if url.startswith("https://firebaseappcheck.googleapis.com/"):
            seen["proofs"].append(url)
            if firebase is not None and (response := firebase(len(seen["proofs"]))):
                return response
            return json_response({"token": proof, "ttl": "3600s"})
        if method == "GET":
            seen["states"].append(parse_qs(urlsplit(url).query)["state"][0])
            return Response(200, {}, PASSWORD_PAGE)
        if url == IDENTITY + "/connect/token":
            seen["exchanges"].append(kwargs)
            return json_response(
                {"access_token": "session-access", "refresh_token": "session-refresh"}
                | {"expires_in": 3600}
            )
        if "Password" in kwargs["data"]:
            return Response(200, {}, MFA_PAGE)
        seen["codes"].append(kwargs["data"])
        return code_form(seen["states"][-1]) or Response(
            302, {"Location": f"com.myqops://android?code=once&state={seen['states'][-1]}"}, b""
        )

    patches = (
        patch("custom_components.myq.api.http.Http.request", request),
        patch("custom_components.myq.api.android_http.AndroidHttp.request", request),
        patch(
            "custom_components.myq.api.firebase.FirebaseRegistration.read",
            return_value=registration,
        ),
    )
    return seen, proof, patches


async def test_code_form_rate_limit_keeps_the_code_for_after_the_cooldown(
    hass, cloud, registration, freezer
):
    limited = True
    seen, _proof, patches = mfa_cloud(
        registration,
        code_form=lambda _state: Response(429, {"Retry-After": "120"}, b"") if limited else None,
    )
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "mfa"
        for _ in range(2):
            # The second submit is refused here, without another post to MyQ.
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"response": "123456"}
            )
            assert result["step_id"] == "mfa"
            assert result["errors"] == {"base": "rate_limited"}
        assert len(seen["codes"]) == 1
        freezer.tick(121)
        limited = False
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
    assert result["type"] == "create_entry"
    assert result["data"]["tokens"]["access_token"] == "session-access"
    # The same code and login were used: no new sign-in page, password or proof.
    assert len(seen["codes"]) == 2 and seen["codes"][0] == seen["codes"][1]
    assert len(seen["states"]) == 1 and len(seen["proofs"]) == 1


async def test_app_check_rate_limit_after_the_code_returns_to_credentials(hass, registration):
    seen, proof, patches = mfa_cloud(
        registration,
        code_form=lambda _state: None,
        firebase=lambda count: Response(429, {"Retry-After": "600"}, b"") if count > 1 else None,
    )
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "mfa"
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        # Stale by the time MyQ returns the code, so the exchange asks Firebase again.
        flow._provider._proof = AppCheckProof(proof, time.time() + 200)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
        # MyQ accepted the code, so there is no challenge left to answer.
        assert result["step_id"] == "password"
        assert result["errors"] == {"base": "rate_limited"}
        assert flow._login is None
    assert len(seen["proofs"]) == 2 and not seen["exchanges"]
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_password_retries_reuse_the_firebase_proof(hass, firebase_exchange):
    with patch(
        "custom_components.myq.api.auth.Login.start",
        AsyncMock(side_effect=AuthenticationError("synthetic wrong password")),
    ) as start:
        result = await start_sign_in(hass)
        for _ in range(3):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
            assert result["errors"] == {"base": "invalid_auth"}
        assert start.await_count == 3
        firebase_exchange.assert_awaited_once()
        # Another registration needs its own proof.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, "debug_token": "private-other-debug"}
        )
        assert firebase_exchange.await_count == 2
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_sign_in_cooldown_skips_firebase_and_stays_with_its_account(hass, firebase_exchange):
    other = "other@example.invalid"
    calls = []

    async def start(login, username, password):
        calls.append(username)
        if username == CREDENTIALS["username"]:
            # As the transport does: record MyQ's 429 for the sign-in site, then raise it.
            error = RateLimited(900, server_retry_after=True)
            login.http.backoff.record(IDENTITY + "/connect/authorize", error)
            raise error
        raise AuthenticationError("synthetic refusal")

    with patch("custom_components.myq.api.auth.Login.start", start):
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["errors"] == {"base": "rate_limited"}
        hass.config_entries.flow.async_abort(result["flow_id"])
        # Restarting setup for that account waits out MyQ's cooldown without
        # spending a Firebase exchange first.
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, "username": CREDENTIALS["username"].upper()}
        )
        assert result["errors"] == {"base": "rate_limited"}
        firebase_exchange.assert_awaited_once()
        hass.config_entries.flow.async_abort(result["flow_id"])
        # Another account's sign-in is not held up.
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, "username": other}
        )
        assert result["errors"] == {"base": "invalid_auth"}
    assert calls == [CREDENTIALS["username"], other]
    assert firebase_exchange.await_count == 2
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_firebase_cooldown_survives_retries_and_restarting_setup(hass, registration, freezer):
    from .test_http import chunks

    session = MagicMock()
    response = session.request.return_value.__aenter__.return_value
    response.status = 429
    response.headers = {"Retry-After": "600"}
    response.content.iter_chunked = MagicMock(return_value=chunks(b"private-error"))

    @asynccontextmanager
    async def request_session(_http, _native):
        yield session

    with (
        patch("custom_components.myq.api.http.Http._request_session", request_session),
        patch(
            "custom_components.myq.api.auth.Login.start",
            AsyncMock(side_effect=AuthenticationError("synthetic login refusal")),
        ) as start,
    ):
        result = await start_sign_in(hass)
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
            assert result["errors"] == {"base": "rate_limited"}
        hass.config_entries.flow.async_abort(result["flow_id"])
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["errors"] == {"base": "rate_limited"}
        session.request.assert_called_once()
        start.assert_not_awaited()
        # Expiring the original deadline permits a new preflight, then login.
        freezer.tick(601)
        response.status = 200
        response.headers = {}
        response.content.iter_chunked = MagicMock(
            return_value=chunks(
                json_response({"token": firebase_token(registration), "ttl": "3600s"}).body
            )
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["errors"] == {"base": "invalid_auth"}
        assert session.request.call_count == 2
        start.assert_awaited_once()
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    "registration_source", ["file", "form", "json_string", "json_line", "json_object", "full_json"]
)
async def test_default_form_gets_firebase_proof_and_persists_only_session(
    hass, cloud, registration, app_check_registration_file, registration_source, caplog
):
    credentials = dict(CREDENTIALS)
    if registration_source != "file":
        app_check_registration_file.unlink()
        credentials["debug_token"] = {
            "form": "  private-entered-debug  ",
            "json_string": '"private-entered-debug",',
            "json_line": '"debug_token": "private-entered-debug",',
            "json_object": '{"debug_token":"private-entered-debug"}',
            "full_json": json.dumps(
                {**asdict(registration), "debug_token": "private-entered-debug"}, indent=2
            ),
        }[registration_source]
        registration = FirebaseRegistration.from_debug_token("private-entered-debug")
    state, calls = "", []
    proof = firebase_token(registration)

    async def request(_self, method, url, **kwargs):
        nonlocal state
        calls.append(urlsplit(url).netloc)
        if "firebaseappcheck.googleapis.com" not in url:
            assert registration.debug_token not in str(kwargs) + url
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
            assert kwargs["json"] == {"debugToken": registration.debug_token}
            assert registration.resource in url
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
            side_effect=(
                AssertionError("Form token must not depend on a registration file")
                if registration_source != "file"
                else lambda _path: registration
            ),
        ) as read,
    ):
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], credentials)
        assert result["step_id"] == "mfa"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
    assert result["type"] == "create_entry"
    assert set(result["data"]) == {"username", "tokens"}
    assert result["data"]["tokens"]["access_token"] == "session-access"
    assert proof not in str(result["data"])
    assert registration.debug_token not in str(result) + caplog.text
    assert "debug_token" not in result["result"].options
    if registration_source != "file":
        read.assert_not_called()
        assert not app_check_registration_file.exists()
    assert calls[0] == "firebaseappcheck.googleapis.com"
    assert calls.count("firebaseappcheck.googleapis.com") == 1
    assert calls[-1] == "partner-identity.myq-cloud.com"


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
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "password"
    field = "debug_token" if failure == "missing" else "base"
    assert result["errors"] == {field: error_key}
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
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["errors"] == {"debug_token": "app_check_not_configured"}
        start.assert_not_awaited()
    app_check_registration_file.write_text(json.dumps(asdict(registration)))
    requests = []

    async def request(_self, method, url, **kwargs):
        requests.append((method, url))
        return Response(200, {}, PASSWORD_PAGE if method == "GET" else MFA_PAGE)

    with (
        patch("custom_components.myq.api.android_http.AndroidHttp.request", request),
        patch(
            "custom_components.myq.api.http.Http.request",
            AsyncMock(
                return_value=json_response({"token": firebase_token(registration), "ttl": "3600s"})
            ),
        ) as firebase,
    ):
        retry = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert retry["flow_id"] == result["flow_id"]
    assert retry["step_id"] == "mfa"
    assert [method for method, _ in requests] == ["GET", "POST"]
    firebase.assert_awaited_once()
    hass.config_entries.flow.async_abort(retry["flow_id"])
    await hass.async_block_till_done()


@pytest.mark.parametrize("stored_file", ["valid", "malformed", "unreadable"])
async def test_submitted_debug_token_takes_priority_over_registration_file(
    hass, cloud, tokens, app_check_registration_file, stored_file
):
    submitted = "private-entered-debug"
    entered = FirebaseRegistration.from_debug_token(submitted)
    if stored_file == "malformed":
        app_check_registration_file.write_text("malformed registration")
    original_contents = app_check_registration_file.read_bytes()

    async def start(login, _username, _password):
        await login.app_check_provider.acquire()
        login.token_manager.tokens = tokens
        return tokens

    with (
        patch(
            "custom_components.myq.api.firebase.FirebaseRegistration.read",
            side_effect=PermissionError("private-path") if stored_file == "unreadable" else None,
        ) as read,
        patch("custom_components.myq.api.auth.Login.start", start),
        patch(
            "custom_components.myq.api.http.Http.request",
            AsyncMock(
                return_value=json_response({"token": firebase_token(entered), "ttl": "3600s"})
            ),
        ) as firebase,
    ):
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, "debug_token": submitted}
        )
    assert result["type"] == "create_entry"
    read.assert_not_called()
    assert firebase.call_args.kwargs["json"] == {"debugToken": submitted}
    assert app_check_registration_file.read_bytes() == original_contents
    assert submitted not in str(result)


@pytest.mark.parametrize("submitted", ["", " \t\n "])
async def test_empty_debug_token_without_file_stops_before_password_or_mfa(
    hass, app_check_registration_file, submitted
):
    app_check_registration_file.unlink()
    with patch("custom_components.myq.api.auth.Login.start", new_callable=AsyncMock) as start:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, "debug_token": submitted}
        )
    assert result["errors"] == {"debug_token": "app_check_not_configured"}
    start.assert_not_awaited()
    assert not app_check_registration_file.exists()


@pytest.mark.parametrize(
    "submitted",
    [
        "private invalid",
        "private\ninvalid",
        pytest.param("x" * 1025, id="overlong-token"),
        "非ASCII",
        pytest.param('"private-unclosed', id="unclosed-string"),
        pytest.param('{"debug_token":"private-debug"', id="unclosed-object"),
        pytest.param('{"debug_token":""}', id="empty-json-token"),
        pytest.param('{"debug_token":null}', id="null-json-token"),
        pytest.param('{"debug_token":123}', id="numeric-json-token"),
        pytest.param('{"wrong_key":"private-debug"}', id="missing-json-token"),
        pytest.param(
            json.dumps({"debug_token": "private-debug", "padding": "x" * MAX_REGISTRATION_BYTES}),
            id="oversized-object",
        ),
        pytest.param(
            '{"debug_token":' + "[" * 2000 + '"private-debug"' + "]" * 2000 + "}",
            id="deeply-nested-object",
        ),
    ],
)
async def test_invalid_debug_token_is_not_sent_saved_or_echoed(hass, submitted, caplog):
    with (
        patch("custom_components.myq.api.auth.Login.start", new_callable=AsyncMock) as start,
        patch("custom_components.myq.api.http.Http.request", new_callable=AsyncMock) as firebase,
        patch("custom_components.myq.api.firebase.FirebaseRegistration.read") as read,
    ):
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, "debug_token": submitted}
        )
    assert result["step_id"] == "password"
    assert result["errors"] == {"debug_token": "app_check_invalid"}
    assert "debug_token" not in result["data_schema"](CREDENTIALS)
    assert submitted not in str(result) + caplog.text
    start.assert_not_awaited()
    firebase.assert_not_awaited()
    read.assert_not_called()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_reauth_accepts_debug_token_without_file_and_does_not_persist_it(
    hass, cloud, tokens, app_check_registration_file, caplog
):
    app_check_registration_file.unlink()
    submitted = "private-entered-debug"
    entered = FirebaseRegistration.from_debug_token(submitted)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PROFILE["user_id"],
        data={"username": PROFILE["email"], "tokens": {"access_token": "old-session"}},
    )
    entry.add_to_hass(hass)
    result = await choose(hass, await entry.start_reauth_flow(hass))

    async def start(login, username, password):
        assert (username, password) == (CREDENTIALS["username"], CREDENTIALS["password"])
        await login.app_check_provider.acquire()
        login.token_manager.tokens = tokens
        return tokens

    with (
        patch("custom_components.myq.api.auth.Login.start", start),
        patch(
            "custom_components.myq.api.http.Http.request",
            AsyncMock(
                return_value=json_response({"token": firebase_token(entered), "ttl": "3600s"})
            ),
        ) as firebase,
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDENTIALS, "debug_token": submitted}
        )
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data == {"username": PROFILE["email"], "tokens": tokens.as_dict()}
    assert entry.options == {}
    assert firebase.call_args.kwargs["json"] == {"debugToken": submitted}
    assert submitted not in str(entry.data) + str(result) + caplog.text
    assert not app_check_registration_file.exists()


async def test_form_authorization_uses_home_assistant_language(hass, firebase_exchange):
    hass.config.language, hass.config.country = "fr", "CA"
    urls = []

    async def start(login, username, password):
        urls.append(login.authorize_url)
        raise AuthenticationError("stop before network")

    with patch("custom_components.myq.api.auth.Login.start", start):
        result = await start_sign_in(hass)
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


# --- Browser sign-in -------------------------------------------------------------------

CHALLENGE = Response(403, {"cf-mitigated": "challenge"}, b"<title>Just a moment...</title>")


def browser_cloud(registration, *, exchange=None, first_page=CHALLENGE):
    """Offline sign-in whose hosted pages only a browser can pass; counts what is sent."""
    proof = firebase_token(registration)
    seen = {"pages": [], "proofs": [], "exchanges": []}

    async def request(_self, method, url, **kwargs):
        if url.startswith("https://firebaseappcheck.googleapis.com/"):
            seen["proofs"].append(url)
            return json_response({"token": proof, "ttl": "3600s"})
        if url == IDENTITY + "/connect/token":
            seen["exchanges"].append(kwargs)
            return exchange or json_response(
                {"access_token": "browser-access", "refresh_token": "browser-refresh"}
                | {"expires_in": 3600}
            )
        seen["pages"].append((method, kwargs.get("data")))
        return first_page

    patches = (
        patch("custom_components.myq.api.http.Http.request", request),
        patch("custom_components.myq.api.android_http.AndroidHttp.request", request),
        patch(
            "custom_components.myq.api.firebase.FirebaseRegistration.read",
            return_value=registration,
        ),
    )
    return seen, proof, patches


def link_state(result):
    return parse_qs(urlsplit(result["description_placeholders"]["authorize_url"]).query)["state"][0]


def app_address(state, **extra):
    query = {"code": "browser-code", "scope": "openid", "state": state, "iss": IDENTITY, **extra}
    return "com.myqops://android?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())


async def test_browser_check_switches_to_browser_sign_in_and_creates_the_entry(
    hass, cloud, registration
):
    seen, proof, patches = browser_cloud(registration)
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "browser"
        assert result["errors"] == {"base": "browser_required"}
        assert list(result["data_schema"].schema) == ["callback"]
        assert set(result["description_placeholders"]) == {"authorize_url"}
        link = urlsplit(result["description_placeholders"]["authorize_url"])
        assert f"{link.scheme}://{link.netloc}{link.path}" == IDENTITY + "/connect/authorize"
        # Only the first page was asked for: the password never left Home Assistant.
        assert seen["pages"] == [("GET", None)]
        message = (
            f"Failed to launch '{app_address(link_state(result))}' because the scheme does "
            "not have a registered handler."
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": message}
        )
    assert result["type"] == "create_entry"
    assert result["data"]["tokens"]["access_token"] == "browser-access"
    assert "private-password" not in str(result)
    (exchange,) = seen["exchanges"]
    assert exchange["headers"]["Firebase-AppCheck-Token"] == proof
    assert b"code=browser-code" in exchange["data"]
    assert len(seen["proofs"]) == 1  # The proof from before the sign-in pages is reused.


async def test_browser_sign_in_from_the_menu_checks_the_token_first_and_creates_the_entry(
    hass, cloud, registration
):
    seen, proof, patches = browser_cloud(registration)
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass, "browser_start")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "browser" and result["errors"] == {}
        # Firebase accepted the token before the link was shown; MyQ was not contacted.
        assert len(seen["proofs"]) == 1 and seen["pages"] == []
        link = urlsplit(result["description_placeholders"]["authorize_url"])
        assert f"{link.scheme}://{link.netloc}{link.path}" == IDENTITY + "/connect/authorize"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": app_address(link_state(result))}
        )
    assert result["type"] == "create_entry"
    # The account comes from MyQ's profile: no email was entered.
    assert result["title"] == result["data"]["username"] == PROFILE["email"]
    assert result["data"]["tokens"]["access_token"] == "browser-access"
    (exchange,) = seen["exchanges"]
    assert exchange["headers"]["Firebase-AppCheck-Token"] == proof
    assert len(seen["proofs"]) == 1 and seen["pages"] == []


@pytest.mark.parametrize(
    "submitted,firebase_status,errors",
    [
        ("", None, {"debug_token": "app_check_not_configured"}),
        ("private invalid", None, {"debug_token": "app_check_invalid"}),
        ("private-revoked-debug", 403, {"base": "app_check_integrity"}),
    ],
    ids=["no-token-or-file", "invalid-token", "refused-token"],
)
async def test_browser_sign_in_from_the_menu_shows_no_link_until_the_token_works(
    hass, app_check_registration_file, submitted, firebase_status, errors, caplog
):
    app_check_registration_file.unlink()
    with (
        patch(
            "custom_components.myq.api.http.Http.request",
            AsyncMock(return_value=json_response({"error": "private-debug"}, firebase_status)),
        ) as firebase,
        patch(
            "custom_components.myq.api.android_http.AndroidHttp.request", new_callable=AsyncMock
        ) as myq,
    ):
        result = await start_sign_in(hass, "browser_start")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"debug_token": submitted}
        )
    assert result["step_id"] == "browser_start"
    assert result["errors"] == errors
    assert "private" not in str(result) + caplog.text
    assert firebase.await_count == (1 if firebase_status else 0)
    myq.assert_not_awaited()
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._browser is None
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "unique_id,reason",
    [("verified-user", "reauth_successful"), ("other-user", "unique_id_mismatch")],
)
async def test_browser_sign_in_from_the_reconnect_menu_keeps_the_account(
    hass, cloud, registration, unique_id, reason
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={"username": PROFILE["email"], "tokens": {"access_token": "old-session"}},
    )
    entry.add_to_hass(hass)
    _seen, _proof, patches = browser_cloud(registration)
    with patches[0], patches[1], patches[2]:
        result = await choose(hass, await entry.start_reauth_flow(hass), "browser_start")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": app_address(link_state(result))}
        )
    assert result["type"] == "abort" and result["reason"] == reason
    saved = "browser-access" if reason == "reauth_successful" else "old-session"
    assert entry.data["tokens"]["access_token"] == saved


async def test_browser_sign_in_reconnects_the_same_account(hass, cloud, registration):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="verified-user", data={"username": PROFILE["email"]}
    )
    entry.add_to_hass(hass)
    seen, _proof, patches = browser_cloud(registration)
    with patches[0], patches[1], patches[2]:
        result = await choose(hass, await entry.start_reauth_flow(hass))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "private-password"}
        )
        assert result["step_id"] == "browser"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": app_address(link_state(result))}
        )
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["tokens"]["access_token"] == "browser-access"


@pytest.mark.parametrize(
    "pasted",
    [
        "the address was not copied",
        app_address("another-links-state"),
        "https://partner-identity.myq-cloud.com/connect/authorize/callback",
    ],
    ids=["no-address", "other-link", "web-address"],
)
async def test_browser_sign_in_refuses_other_text_and_keeps_the_link(hass, registration, pasted):
    seen, _proof, patches = browser_cloud(registration)
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        state = link_state(result)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": pasted}
        )
    assert result["step_id"] == "browser"
    assert result["errors"] == {"callback": "invalid_callback"}
    assert link_state(result) == state
    assert not seen["exchanges"]
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_expired_browser_link_is_replaced_without_an_exchange(hass, registration):
    seen, _proof, patches = browser_cloud(registration)
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        state = link_state(result)
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        flow._browser.expires = time.monotonic() - 1
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": app_address(state)}
        )
    assert result["errors"] == {"base": "browser_expired"}
    assert link_state(result) != state
    assert not seen["exchanges"]
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_refused_browser_sign_in_needs_a_new_link(hass, registration):
    refused = json_response({"error": "invalid_grant"}, 400)
    seen, _proof, patches = browser_cloud(registration, exchange=refused)
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        first = link_state(result)
        # MyQ ended the sign-in itself: no code to send.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": app_address(first, error="access_denied")}
        )
        assert result["errors"] == {"base": "browser_auth_failed"}
        assert not seen["exchanges"]
        second = link_state(result)
        assert second != first
        # The code is refused once sent, and is not sent again.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": app_address(second)}
        )
        assert result["errors"] == {"base": "browser_auth_failed"}
        assert link_state(result) not in {first, second}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": app_address(second)}
        )
        assert result["errors"] == {"callback": "invalid_callback"}
    assert len(seen["exchanges"]) == 1
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_browser_check_at_the_code_switches_to_browser_sign_in(hass, registration):
    seen, _proof, patches = mfa_cloud(
        registration,
        code_form=lambda _state: Response(403, {"cf-mitigated": "challenge"}, b""),
    )
    with patches[0], patches[1], patches[2]:
        result = await start_sign_in(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "mfa"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
    assert result["step_id"] == "browser"
    assert result["errors"] == {"base": "browser_required"}
    # A fresh link: the hosted sign-in's own state is not reused.
    assert link_state(result) not in seen["states"]
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._login is None
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_browser_step_without_a_debug_token_asks_for_one(hass):
    result = await start_sign_in(hass)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    result = await flow.async_step_browser()
    assert result["step_id"] == "browser_start"
    hass.config_entries.flow.async_abort(result["flow_id"])

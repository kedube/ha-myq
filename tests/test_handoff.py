"""Retained research helpers for PKCE transactions and private browser handoffs."""

import importlib.util
import json
import stat
import time
from pathlib import Path

import pytest

from custom_components.myq.api.app_check import AppCheckProof
from custom_components.myq.api.errors import (
    AuthenticationError,
    ProtocolError,
)
from custom_components.myq.api.handoff import (
    BrowserAuthorization,
    create_request,
    create_result,
    read_result,
)
from custom_components.myq.api.oauth import OAuthSession

from .conftest import app_check_jwt


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


def test_helper_accepts_the_home_assistant_language():
    browser = BrowserAuthorization(create_request(OAuthSession(locale="pt-BR")))
    assert "ui_locales=pt-BR" in browser.authorize_url

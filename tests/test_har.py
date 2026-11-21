"""A HAR may provide only the callback for the probe's own fresh transaction."""

import json

import pytest

from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import (
    AuthenticationError,
    ProtocolError,
    UnsupportedAuthStep,
)
from custom_components.myq.api.har import read_callback
from custom_components.myq.api.oauth import OAuthSession


def redirect(url, source=IDENTITY + "/connect/authorize/callback"):
    return {
        "request": {"url": source, "postData": {"text": "Password=private-secret"}},
        "response": {
            "status": 302,
            "headers": [
                {"name": "LOCATION", "value": url},
                {"name": "Set-Cookie", "value": "private-secret"},
            ],
        },
    }


def capture(tmp_path, entries):
    path = tmp_path / "synthetic.har"
    path.write_text(json.dumps({"log": {"entries": entries}}))
    return path


def test_current_callback_is_selected_without_reusing_capture_credentials(tmp_path):
    oauth = OAuthSession()
    url = "com.myqops://android?code=synthetic-code&state=" + oauth.state
    path = capture(
        tmp_path,
        [
            redirect("com.myqops://android?code=stale-code&state=old-state"),
            redirect(url),
            redirect(url),
        ],
    )
    assert read_callback(path, oauth) == url
    assert not oauth.used  # Only OAuthSession.complete may consume the code.
    assert not hasattr(oauth, "password")


@pytest.mark.parametrize("kind", ["website", "token_body", "untrusted_origin", "status_200"])
def test_only_identity_origin_redirects_count_as_callbacks(tmp_path, kind):
    oauth = OAuthSession()
    url = "com.myqops://android?code=synthetic-code&state=" + oauth.state
    entry = redirect(url)
    if kind == "website":
        entry = redirect("/Home/Secure", source=IDENTITY + "/AccountMfa/VerifyOtp")
    elif kind == "token_body":
        entry = {
            "request": {"url": IDENTITY + "/connect/token"},
            "response": {"status": 200, "content": {"text": '{"access_token":"private"}'}},
        }
    elif kind == "untrusted_origin":
        entry["request"]["url"] = "https://untrusted.invalid/redirect"
    else:
        entry["response"]["status"] = 200
    with pytest.raises(UnsupportedAuthStep, match="no MyQ OAuth callback"):
        read_callback(capture(tmp_path, [entry]), oauth)


def test_wrong_state_is_not_exchanged(tmp_path):
    with pytest.raises(AuthenticationError, match="does not match"):
        read_callback(
            capture(tmp_path, [redirect("com.myqops://android?code=private-code&state=old")]),
            OAuthSession(),
        )


def test_ambiguous_callbacks_are_rejected(tmp_path):
    oauth = OAuthSession()
    entries = [
        redirect(f"com.myqops://android?code={code}&state={oauth.state}") for code in ["one", "two"]
    ]
    with pytest.raises(ProtocolError, match="multiple OAuth callbacks"):
        read_callback(capture(tmp_path, entries), oauth)


def test_oauth_error_is_not_treated_as_a_code(tmp_path):
    oauth = OAuthSession()
    url = f"com.myqops://android?error=access_denied&state={oauth.state}"
    with pytest.raises(AuthenticationError, match="not completed"):
        read_callback(capture(tmp_path, [redirect(url)]), oauth)


@pytest.mark.parametrize("used", [False, True])
def test_expired_or_consumed_transaction_does_not_read_har(tmp_path, used):
    oauth = OAuthSession()
    if used:
        oauth.used = True
    else:
        oauth.expires = 0
    with pytest.raises(AuthenticationError):
        read_callback(tmp_path / "does-not-exist.har", oauth)


@pytest.mark.parametrize("data", [b"private-secret", b"[]", b'{"log":{"entries":{}}}'])
def test_malformed_capture_errors_do_not_expose_content(tmp_path, data):
    path = tmp_path / "invalid.har"
    path.write_bytes(data)
    with pytest.raises(ProtocolError) as caught:
        read_callback(path, OAuthSession())
    assert "private-secret" not in str(caught.value)


def test_capture_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("custom_components.myq.api.har.MAX_HAR_BYTES", 4)
    path = tmp_path / "large.har"
    path.write_bytes(b"12345")
    with pytest.raises(ProtocolError, match="capture limit"):
        read_callback(path, OAuthSession())

"""Prove that native capture output excludes account secrets and other hosts."""

import json
import re
from types import SimpleNamespace

from scripts.capture_native import (
    ALLOW_HOSTS,
    FIREBASE,
    IDENTITY,
    AuthCapture,
    endpoint,
    public_form,
)


def flow(host, path, body=b"", response=None, headers=None):
    raw = json.dumps(response or {}).encode()
    return SimpleNamespace(
        request=SimpleNamespace(host=host, path=path, raw_content=body, headers=headers or {}),
        response=SimpleNamespace(
            raw_content=raw, status_code=200, get_text=lambda **_: raw.decode()
        ),
    )


def test_capture_never_writes_password_code_or_oauth_tokens(tmp_path, capsys):
    observer = AuthCapture(tmp_path)
    observer.response(flow(IDENTITY, "/Account/Login", b"Password=private-password"))
    observer.response(
        flow(
            IDENTITY,
            "/connect/token",
            b"client_id=TEST_IOS&grant_type=authorization_code&code=secret-code"
            b"&code_verifier=secret-verifier&refresh_token=secret-refresh&client_secret=secret-client",
            {"access_token": "secret-access", "refresh_token": "secret-rotation"},
            {"Cookie": "secret-cookie", "Authorization": "Bearer secret-bearer"},
        )
    )
    report = (tmp_path / "sanitized-report.json").read_text()
    output = report + capsys.readouterr().out
    assert "secret-" not in output
    assert "private-password" not in output
    event = json.loads(report)["events"][0]
    assert event["client_id"] == "TEST_IOS"
    assert event["code_present"] and event["refresh_token_returned"]
    assert event["app_check_present"] is False
    assert len(list(tmp_path.iterdir())) == 1


def test_app_check_is_private_and_never_printed(tmp_path, capsys):
    observer = AuthCapture(tmp_path)
    token = "synthetic.header.signature-not-a-real-token"
    observer.response(flow(IDENTITY, "/connect/token", headers={"Firebase-AppCheck-Token": token}))
    observer.response(flow(IDENTITY, "/connect/token", headers={"Firebase-AppCheck-Token": token}))
    assert (tmp_path / "app-check-1.txt").read_text() == token
    assert not (tmp_path / "app-check-2.txt").exists()
    report = (tmp_path / "sanitized-report.json").read_text()
    assert token not in report + capsys.readouterr().out
    for path in tmp_path.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_firebase_provider_detected_without_project_ids(tmp_path, capsys):
    observer = AuthCapture(tmp_path)
    observer.response(
        flow(
            FIREBASE,
            "/v1/projects/private-project/apps/private-app:exchangeAppAttestAssertion?key=private-key",
            b'{"assertion":"private-assertion"}',
            {"token": "synthetic.firebase.issued-token"},
        )
    )
    output = (tmp_path / "sanitized-report.json").read_text() + capsys.readouterr().out
    assert "exchangeAppAttestAssertion" in output
    assert "private-" not in output
    assert "synthetic.firebase" not in output
    assert observer.events[0]["app_check_saved"]


def test_host_scope_and_unknown_routes_are_excluded(tmp_path):
    pattern = re.compile(ALLOW_HOSTS[0])
    assert pattern.fullmatch(f"{IDENTITY}:443")
    assert pattern.fullmatch(f"{FIREBASE}:443")
    for host in ["example.com", f"{IDENTITY}.example.com", f"fake.{IDENTITY}"]:
        assert not pattern.fullmatch(f"{host}:443")
        assert endpoint(host, "/connect/token") is None
    observer = AuthCapture(tmp_path)
    observer.response(flow("example.com", "/connect/token", b"Password=private-password"))
    observer.response(flow(IDENTITY, "/AccountMfa/VerifyOtp?Otp=123456"))
    assert not list(tmp_path.iterdir())


def test_redirect_query_and_fragment_are_not_saved():
    result = public_form(
        "redirect_uri=com.test%3A%2F%2Fios%3Fcode%3Dprivate-code%23private-fragment"
    )
    assert result["redirect_uri"] == "com.test://ios"
    assert "private-" not in json.dumps(result)


def test_invalid_and_oversized_token_not_saved(tmp_path):
    observer = AuthCapture(tmp_path)
    for token in ["", "abc\r\nsecret-header", "x" * 16385, None, {"token": "secret"}]:
        assert observer.save_token(token) is False
    assert not list(tmp_path.iterdir())


def test_connection_diagnostics_exclude_addresses_queries_and_unrelated_hosts(tmp_path):
    observer = AuthCapture(tmp_path)
    observer.client_connected(SimpleNamespace(peername=("192.0.2.123", 12345)))
    observer.http_connect(flow("unrelated-private-site.example", "/?secret=query"))
    observer.http_connect(flow("other-identity.myq-cloud.com", "/?secret=query"))
    observer.request(flow("mitm.it", "/?secret=query"))
    observer.request(flow(IDENTITY, "/Account/Login", b"Password=secret-password"))
    report = (tmp_path / "sanitized-report.json").read_text()
    assert "secret" not in report
    assert "192.0.2.123" not in report
    assert "unrelated-private-site" not in report
    diagnostics = json.loads(report)["connection_diagnostics"]
    assert diagnostics == {
        "client_connections": 1,
        "certificate_page_requests": 1,
        "decrypted_auth_host_requests": 1,
        "myq_connect_hosts": ["other-identity.myq-cloud.com"],
        "tls_check_requests": 0,
        "tls_checks_from_other_devices": 0,
        "direct_tls_check_requests": 0,
        "http_tls_check_redirects": 0,
    }


def test_local_certificate_host_counted(tmp_path):
    observer = AuthCapture(tmp_path, onboarding_host="192.0.2.10")
    observer.request(flow("192.0.2.10", "/cert/cer"))
    observer.request(flow("mitm.it", "/"))
    assert observer.certificate_page_requests == 1


def test_tls_failure_is_classified_without_raw_error_or_address(tmp_path, capsys):
    observer = AuthCapture(tmp_path, onboarding_host="192.0.2.10")
    observer.tls_failed_client(
        SimpleNamespace(
            conn=SimpleNamespace(error="tls alert unknown ca secret-error 192.0.2.20"),
            context=SimpleNamespace(
                server=SimpleNamespace(address=(IDENTITY, 443)),
                client=SimpleNamespace(sni=IDENTITY, peername=("192.0.2.20", 23456)),
            ),
        )
    )
    text = (tmp_path / "sanitized-report.json").read_text() + capsys.readouterr().out
    assert "secret-error" not in text
    assert "192.0.2.20" not in text
    assert observer.tls_failures == [
        {
            "side": "client",
            "host_role": "myq_identity",
            "reason": "unknown_ca",
            "from_other_device": True,
        }
    ]

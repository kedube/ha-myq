"""Synthetic Firebase exchange: registration, expiration, isolation and redaction."""

import asyncio
import base64
import json
import os
import time
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.myq.api.app_check import AppCheckProof
from custom_components.myq.api.const import ANDROID_PACKAGE, FIREBASE_USER_AGENT
from custom_components.myq.api.errors import (
    AppCheckConfigInvalid,
    AppCheckConfigUnreadable,
    AppCheckExpired,
    AppCheckIntegrityError,
    AppCheckInvalid,
    AppCheckNotConfigured,
    AppCheckUnavailable,
)
from custom_components.myq.api.firebase import (
    FirebaseDebugAppCheckProvider,
    FirebaseRegistration,
)
from custom_components.myq.api.oauth import OAuthSession
from custom_components.myq.api.tokens import TokenManager

from .conftest import json_response


def firebase_token(registration, **claims):
    payload = {
        "exp": time.time() + 3600,
        "sub": registration.application,
        "aud": [f"projects/{registration.application.split(':')[1]}"],
        **claims,
    }
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"e30.{part}.fake-signature"


def test_form_registration_uses_public_metadata_and_keeps_token_out_of_repr():
    profile = FirebaseRegistration.from_debug_token("  private-entered-debug\n")
    assert profile.debug_token == "private-entered-debug"
    assert profile.package == ANDROID_PACKAGE
    assert profile.resource.endswith(":exchangeDebugToken")
    assert "private-entered-debug" not in repr(profile)


@pytest.mark.parametrize(
    "token", [None, 123, "", "  ", "private invalid", "private\ninvalid", "x" * 1025]
)
def test_form_registration_rejects_invalid_tokens_without_disclosing_them(token):
    with pytest.raises(AppCheckInvalid) as error:
        FirebaseRegistration.from_debug_token(token)
    assert "private" not in str(error.value)


async def test_registered_exchange_cache_and_oauth_header_scope(http, registration):
    token = firebase_token(registration)
    http.request.return_value = json_response({"token": token, "ttl": "3600.0s"})
    provider = FirebaseDebugAppCheckProvider(http, registration)
    results = await asyncio.gather(*(provider.acquire() for _ in range(5)))
    assert all(p.token == token for p in results)
    request = http.request.call_args
    http.request.assert_awaited_once()
    assert request.args == (
        "POST",
        "https://firebaseappcheck.googleapis.com/v1/projects/test-project/apps/"
        "1:123456:android:abcdef:exchangeDebugToken?key=private-key",
    )
    assert request.kwargs["json"] == {"debugToken": "private-debug"}
    assert request.kwargs["headers"] == {
        "User-Agent": FIREBASE_USER_AGENT,
        "Content-Type": "application/json",
        "X-Android-Package": ANDROID_PACKAGE,
        "X-Android-Cert": "AB" * 20,
    }
    native = AsyncMock()
    native.request.return_value = json_response(
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
    )
    manager, oauth = TokenManager(native), OAuthSession()
    await oauth.complete(
        f"com.myqops://android?code=one-use&state={oauth.state}",
        manager,
        app_check_provider=provider,
    )
    headers = native.request.call_args.kwargs["headers"]
    assert headers["Firebase-AppCheck-Token"] == token
    assert not any(k.startswith("X-Android-") for k in headers)
    assert "private-debug" not in str(native.request.call_args)
    await manager.access_token(rejected_token="access")
    assert "Firebase-AppCheck-Token" not in native.request.call_args.kwargs["headers"]
    assert "private-" not in repr(registration) + repr(provider) + repr(results)
    provider._proof = AppCheckProof(token, time.time() + 200)
    await provider.acquire()
    assert http.request.await_count == 2
    provider.invalidate()
    await provider.acquire()
    assert http.request.await_count == 3


@pytest.mark.parametrize("ttl", ["300s", "0s", "-1s", "NaNs", "Infinitys", "3600", None])
async def test_ttl_bounds_even_a_long_lived_jwt(http, registration, ttl):
    http.request.return_value = json_response({"token": firebase_token(registration), "ttl": ttl})
    with pytest.raises((AppCheckInvalid, AppCheckExpired)):
        await FirebaseDebugAppCheckProvider(http, registration).acquire()


@pytest.mark.parametrize("claims", [{"sub": "another-app"}, {"aud": ["projects/999"]}])
async def test_wrong_app_or_project_is_not_sent_to_myq(http, registration, claims):
    http.request.return_value = json_response(
        {"token": firebase_token(registration, **claims), "ttl": "3600s"}
    )
    with pytest.raises(AppCheckInvalid):
        await FirebaseDebugAppCheckProvider(http, registration).acquire()


@pytest.mark.parametrize("status", [302, 400, 401, 403, 500])
async def test_rejections_do_not_disclose_server_body_or_replay(http, registration, status):
    http.request.return_value = json_response({"error": "private-debug"}, status)
    with pytest.raises((AppCheckUnavailable, AppCheckIntegrityError)) as error:
        await FirebaseDebugAppCheckProvider(http, registration).acquire()
    assert "private-" not in str(error.value)
    http.request.assert_awaited_once()


async def test_file_is_private_input_reloaded_after_invalidation(http, registration, tmp_path):
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(asdict(registration)))
    provider = FirebaseDebugAppCheckProvider(http, path)
    http.request.return_value = json_response(
        {"token": firebase_token(registration), "ttl": "3600s"}
    )
    with patch.object(FirebaseRegistration, "read", wraps=FirebaseRegistration.read) as reader:
        await provider.acquire()
        await provider.acquire()
        reader.assert_called_once_with(path)
        path.unlink()
        provider.invalidate()
        with pytest.raises(AppCheckNotConfigured):
            await provider.acquire()
    http.request.assert_awaited_once()


def test_missing_registration_is_distinct_from_provider_unavailability(tmp_path):
    with pytest.raises(AppCheckNotConfigured) as error:
        FirebaseRegistration.read(tmp_path / "missing.json")
    assert error.value.reason == "not_configured"


def test_unreadable_registration_redacts_os_error(tmp_path):
    with (
        patch(
            "custom_components.myq.api.firebase.os.open",
            side_effect=PermissionError("private-path"),
        ),
        pytest.raises(AppCheckConfigUnreadable) as error,
    ):
        FirebaseRegistration.read(tmp_path / "registration.json")
    assert error.value.reason == "config_unreadable"
    assert "private-" not in str(error.value)
    assert error.value.__suppress_context__


def test_malformed_or_nonregular_registration_redacts_values(tmp_path, registration):
    path = tmp_path / "registration.json"
    for body in (
        '{"debug_token":"private-debug"}',
        '["private-debug"]',
        "x" * 16385,
        json.dumps({**asdict(registration), "certificate": "private-bad-certificate"}),
    ):
        path.write_text(body)
        with pytest.raises(AppCheckConfigInvalid) as error:
            FirebaseRegistration.read(path)
        assert error.value.reason == "config_invalid"
        assert "private-" not in str(error.value)
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(AppCheckConfigInvalid):
            FirebaseRegistration.read(fifo)

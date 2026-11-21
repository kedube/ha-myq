"""Proof freshness, redaction, acquisition lifecycle and single-use exchanges."""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock

import pytest

from custom_components.myq.api.app_check import (
    AppCheckProof,
    CachedAppCheckProvider,
    FileAppCheckProvider,
    SuppliedAppCheckProvider,
)
from custom_components.myq.api.errors import (
    AppCheckExpired,
    AppCheckIntegrityError,
    AppCheckInvalid,
    AppCheckUnavailable,
    ApplicationVerificationError,
    AuthenticationError,
)
from custom_components.myq.api.oauth import OAuthSession
from custom_components.myq.api.tokens import TokenManager
from custom_components.myq.config_flow import error_key

from .conftest import app_check_jwt, json_response


@pytest.mark.parametrize("value", ["", "x.y.z", "one\nline", None, "x" * 16385])
def test_bad_proof_has_fixed_error(value):
    with pytest.raises(AppCheckInvalid) as error:
        AppCheckProof.from_token(value)
    assert error_key(error.value) == "app_check_invalid"


async def test_expiry_is_required_and_not_trusted_as_authenticity():
    provider = SuppliedAppCheckProvider(app_check_jwt(time.time() + 299))
    with pytest.raises(AppCheckExpired):
        await provider.acquire()
    proof = AppCheckProof.from_token(app_check_jwt())
    assert proof.token not in repr(proof)
    proof.check_fresh()  # Only a freshness hint; the fake signature is not verified here.


async def test_file_refreshes_and_rejection_invalidates_cache(tmp_path):
    path = tmp_path / "proof.token.json"
    old, new = app_check_jwt(label="old"), app_check_jwt(label="new")
    path.write_text(json.dumps({"token": old}))
    provider = FileAppCheckProvider(path)
    assert (await provider.acquire()).token == old
    path.write_text(json.dumps({"token": new}))
    provider.invalidate()
    assert (await provider.acquire()).token == new
    provider._proof = AppCheckProof(old, time.time() + 10)
    assert (await provider.acquire()).token == new
    path.write_text(new)  # Compatible with the native capture helper's raw files.
    provider.invalidate()
    assert (await provider.acquire()).token == new


async def test_file_unavailable_oversized_or_nonregular_fails_safely(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(AppCheckUnavailable):
        await FileAppCheckProvider(path).acquire()
    path.write_text("x" * 32769)
    with pytest.raises(AppCheckInvalid):
        await FileAppCheckProvider(path).acquire()
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(AppCheckInvalid):
            await FileAppCheckProvider(fifo, timeout=0.5).acquire()


async def test_concurrent_acquisition_is_shared_and_expiry_reloads():
    loader = AsyncMock(return_value=AppCheckProof.from_token(app_check_jwt()))
    provider = CachedAppCheckProvider(loader)
    results = await asyncio.gather(*(provider.acquire() for _ in range(8)))
    assert len({r.token for r in results}) == 1
    loader.assert_awaited_once()
    provider._proof = AppCheckProof(results[0].token, time.time())
    await provider.acquire()
    assert loader.await_count == 2


async def test_timeout_exception_and_integrity_errors_do_not_leak():
    async def hangs():
        await asyncio.Event().wait()

    provider = CachedAppCheckProvider(hangs, timeout=0.01)
    with pytest.raises(AppCheckUnavailable):
        await provider.acquire()
    provider = CachedAppCheckProvider(AsyncMock(side_effect=ValueError("private-token")))
    with pytest.raises(AppCheckUnavailable) as error:
        await provider.acquire()
    assert "private" not in str(error.value)
    provider = CachedAppCheckProvider(AsyncMock(side_effect=AppCheckIntegrityError()))
    with pytest.raises(AppCheckIntegrityError) as error:
        await provider.acquire()
    assert error_key(error.value) == "app_check_integrity"


async def test_cancelled_acquisition_releases_lock_and_invalidating_inflight_discards_proof():
    entered, release = asyncio.Event(), asyncio.Event()

    async def load():
        entered.set()
        await release.wait()
        return AppCheckProof.from_token(app_check_jwt())

    provider = CachedAppCheckProvider(load)
    task = asyncio.create_task(provider.acquire())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.last_status == "cancelled"
    entered.clear()
    task = asyncio.create_task(provider.acquire())
    await entered.wait()
    provider.invalidate()
    release.set()
    with pytest.raises(AppCheckUnavailable):
        await task
    assert provider._proof is None
    assert await provider.acquire()


async def test_local_failures_allow_new_proof_without_consuming_code(http):
    manager, oauth = TokenManager(http), OAuthSession()
    url = f"com.myqops://android?code=private-code&state={oauth.state}"
    with pytest.raises(AppCheckUnavailable):
        await oauth.complete(url, manager, require_app_check=True)
    with pytest.raises(AppCheckInvalid):
        await oauth.complete(url, manager, app_check_token="bad\nheader")
    with pytest.raises(AppCheckExpired):
        await oauth.complete(
            url,
            manager,
            app_check_provider=SuppliedAppCheckProvider(app_check_jwt(time.time())),
        )
    assert not oauth.used
    http.request.assert_not_awaited()
    http.request.return_value = json_response(
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
    )
    provider = SuppliedAppCheckProvider(app_check_jwt())
    outcomes = await asyncio.gather(
        *(oauth.complete(url, manager, app_check_provider=provider) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(x, AuthenticationError) for x in outcomes) == 1
    http.request.assert_awaited_once()
    assert oauth.used


async def test_expiry_is_rechecked_after_acquisition_and_token_lock(http):
    manager, oauth = TokenManager(http), OAuthSession()
    url = f"com.myqops://android?code=code&state={oauth.state}"

    async def load():
        oauth.expires = 0
        return AppCheckProof.from_token(app_check_jwt())

    with pytest.raises(AuthenticationError):
        await oauth.complete(url, manager, app_check_provider=CachedAppCheckProvider(load))
    assert not oauth.used
    oauth.expires = time.monotonic() + 600
    await manager._lock.acquire()
    task = asyncio.create_task(oauth.complete(url, manager))
    await asyncio.sleep(0)
    oauth.expires = 0
    manager._lock.release()
    with pytest.raises(AuthenticationError):
        await task
    assert not oauth.used
    http.request.assert_not_awaited()


async def test_server_rejection_invalidates_proof_and_consumes_code(http):
    http.request.return_value = json_response({"code": "401.122"}, 401)
    manager, oauth = TokenManager(http), OAuthSession()
    provider = SuppliedAppCheckProvider(app_check_jwt())
    with pytest.raises(ApplicationVerificationError):
        await oauth.complete(
            f"com.myqops://android?code=code&state={oauth.state}",
            manager,
            app_check_provider=provider,
        )
    assert oauth.used
    with pytest.raises(AppCheckUnavailable):
        await provider.acquire()


async def test_proof_expiring_while_queued_does_not_consume_code(http):
    manager, oauth = TokenManager(http), OAuthSession()
    provider = SuppliedAppCheckProvider(app_check_jwt())
    await manager._lock.acquire()
    task = asyncio.create_task(
        oauth.complete(
            f"com.myqops://android?code=code&state={oauth.state}",
            manager,
            app_check_provider=provider,
        )
    )
    await asyncio.sleep(0)
    # Deterministically advance freshness checks beyond the proof lifetime.
    from unittest.mock import patch

    with patch("custom_components.myq.api.app_check.time.time", return_value=time.time() + 7200):
        manager._lock.release()
        with pytest.raises(AppCheckExpired):
            await task
    assert not oauth.used
    http.request.assert_not_awaited()


async def test_cancel_before_proof_is_ready_preserves_transaction(http):
    entered = asyncio.Event()

    async def load():
        entered.set()
        await asyncio.Event().wait()

    manager, oauth = TokenManager(http), OAuthSession()
    url = f"com.myqops://android?code=code&state={oauth.state}"
    task = asyncio.create_task(
        oauth.complete(url, manager, app_check_provider=CachedAppCheckProvider(load))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not oauth.used
    http.request.assert_not_awaited()


async def test_cancel_after_request_starts_never_replays(http):
    entered = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    http.request.side_effect = send
    manager, oauth = TokenManager(http), OAuthSession()
    url = f"com.myqops://android?code=code&state={oauth.state}"
    task = asyncio.create_task(oauth.complete(url, manager))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(AuthenticationError):
        await oauth.complete(url, manager)
    http.request.assert_awaited_once()

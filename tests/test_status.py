"""Read-only status monitor ownership, inventory changes and reconnect limits."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.myq.api import status
from custom_components.myq.api.errors import (
    AccessDenied,
    AuthenticationError,
    CannotConnect,
)


@pytest.fixture
def monitor(monkeypatch):
    instances = []
    connected = asyncio.Queue()

    class Session:
        def __init__(self, aliases, on_status):
            self.aliases = aliases
            self.on_status = on_status
            self.ended = asyncio.Event()
            self.failure = None
            self.closed = asyncio.Event()
            self.token = None
            instances.append(self)

        def update_aliases(self, aliases):
            self.aliases = aliases

        async def subscribe(self, token):
            self.token = token
            connected.put_nowait(self)

        async def wait_closed(self):
            await self.ended.wait()
            if self.failure:
                raise self.failure

        async def close(self):
            self.ended.set()
            self.closed.set()

    monkeypatch.setattr(status, "TendStatusSession", Session)
    client = MagicMock()
    client.auth.access_token = AsyncMock(side_effect=["first-token", "second-token"])
    changes = []
    subject = status.CameraStatusMonitor(client, changes.append, AsyncMock())
    return subject, connected, instances, changes, client


async def test_initially_empty_inventory_starts_when_known_alias_arrives(monitor):
    subject, connected, instances, changes, client = monitor
    await subject.start()
    await asyncio.sleep(0)
    client.auth.access_token.assert_not_awaited()
    subject.update_aliases({"camera"})
    async with asyncio.timeout(1):
        session = await connected.get()
    session.on_status("other-camera")
    session.on_status("camera")
    assert changes == ["camera"]
    subject.update_aliases(set())
    async with asyncio.timeout(1):
        await session.closed.wait()
    assert len(instances) == 1
    subject.update_aliases({"second-camera"})
    async with asyncio.timeout(1):
        second = await connected.get()
    assert second.aliases == {"second-camera"} and second.token == "second-token"
    await subject.close()
    assert second.closed.is_set()
    await subject.resume()
    assert len(instances) == 2


async def test_pause_closes_before_live_view_and_resume_uses_fresh_token(monitor):
    subject, connected, instances, changes, _ = monitor
    subject.update_aliases({"camera"})
    await subject.start()
    first = await connected.get()
    await subject.pause()
    assert first.closed.is_set()
    first.on_status("camera")
    assert not changes
    await subject.resume()
    second = await connected.get()
    assert second.token == "second-token"
    await subject.close()
    assert all(session.closed.is_set() for session in instances)
    assert subject._task is None


async def test_refresh_auth_failure_requests_reauth_without_automatic_retry(monitor):
    subject, _, _, _, client = monitor
    client.auth.access_token.side_effect = AuthenticationError("synthetic expiry")
    subject.update_aliases({"camera"})
    await subject.start()
    async with asyncio.timeout(1):
        await subject._task
    subject._on_error.assert_awaited_once()
    await subject.resume()
    subject.update_aliases({"other"})
    client.auth.access_token.assert_awaited_once()
    await subject.close()


async def test_network_failure_is_backed_off_and_pause_cancels_wait(monitor):
    subject, connected, instances, _, _ = monitor
    subject.update_aliases({"camera"})
    await subject.start()
    first = await connected.get()
    first.failure = CannotConnect("synthetic network loss")
    first.ended.set()
    async with asyncio.timeout(1):
        await first.closed.wait()
    assert len(instances) == 1
    assert status.MIN_RECONNECT_SECONDS >= 60
    await subject.pause()
    assert subject._task is None
    subject._on_error.assert_awaited_once()
    await subject.close()


async def test_pause_during_token_refresh_does_not_open_a_late_session(monitor):
    subject, _, instances, _, client = monitor
    entered = asyncio.Event()

    async def pending_token():
        entered.set()
        await asyncio.Event().wait()

    client.auth.access_token.side_effect = pending_token
    subject.update_aliases({"camera"})
    await subject.start()
    await entered.wait()
    await subject.pause()
    assert not instances
    await subject.close()


async def test_tend_session_denial_backs_off_and_reconnects(monitor, monkeypatch):
    """A kick-out, action 310 or refused Tend login is not a credential failure."""
    monkeypatch.setattr(status, "MIN_RECONNECT_SECONDS", 0)
    monkeypatch.setattr(status, "MAX_RECONNECT_SECONDS", 0)
    subject, connected, _instances, _changes, _client = monitor
    subject.update_aliases({"camera"})
    await subject.start()
    async with asyncio.timeout(1):
        first = await connected.get()
    first.failure = AccessDenied("Tend camera connection was denied or ended")
    first.ended.set()
    async with asyncio.timeout(1):
        second = await connected.get()
    assert second.token == "second-token"
    await subject.close()


async def test_tend_access_denial_waits_for_the_maximum_backoff(monitor, monkeypatch):
    """Retrying a denial must never become a hot reconnect loop."""
    subject, connected, _instances, _changes, _client = monitor
    delays = []
    backed_off = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(seconds, *args, **kwargs):
        if not seconds:  # Plain event-loop yields are unaffected.
            return await real_sleep(seconds, *args, **kwargs)
        delays.append(seconds)
        await subject.close()  # Stop after recording the first reconnect delay.
        backed_off.set()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    subject.update_aliases({"camera"})
    await subject.start()
    async with asyncio.timeout(1):
        first = await connected.get()
    first.failure = AccessDenied("Tend camera login was rejected")
    first.ended.set()
    async with asyncio.timeout(1):
        await backed_off.wait()
    assert delays == [status.MAX_RECONNECT_SECONDS]

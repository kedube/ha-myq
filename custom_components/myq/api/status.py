"""Owned read-only Tend status connection, paused while native live views exist."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from .client import Client
from .errors import AccessDenied, AuthenticationError, CannotConnect, MyQError, RateLimited
from .tend import TendStatusSession, _alias

MIN_RECONNECT_SECONDS = 60
MAX_RECONNECT_SECONDS = 900


class CameraStatusMonitor:
    """No media wake-up, registrations, or optimistic state updates. Only explicit
    siren and preset commands (CameraCommands) are ever sent over it."""

    def __init__(
        self,
        client: Client,
        on_status: Callable[[str], None],
        on_error: Callable[[MyQError], Awaitable[None]],
    ) -> None:
        self.client = client
        self._on_status = on_status
        self._on_error = on_error
        self._aliases: set[str] = set()
        self._session: TendStatusSession | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._paused = True
        self._auth_failed = False
        self._changed = asyncio.Event()
        self._lifecycle = asyncio.Lock()

    def update_aliases(self, aliases: set[str]) -> None:
        if len(aliases) > 4096:
            from .errors import ProtocolError

            raise ProtocolError("Too many camera status aliases")
        validated = {_alias(alias) for alias in aliases}
        if validated != self._aliases:
            self._aliases = validated
            if self._session is not None and validated:
                self._session.update_aliases(validated)
            self._changed.set()

    async def start(self) -> None:
        await self.resume()

    async def resume(self) -> None:
        async with self._lifecycle:
            if self._closed or self._auth_failed:
                return
            self._paused = False
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())

    async def pause(self) -> None:
        async with self._lifecycle:
            self._paused = True
            await self._stop_worker()

    async def close(self) -> None:
        async with self._lifecycle:
            self._closed = self._paused = True
            await self._stop_worker()

    async def _stop_worker(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def command(self, alias: str, command: str, actions: tuple[tuple[str, str], ...]) -> bool:
        """Send over the open status session, as the app does; False if none is open."""
        session = self._session
        if session is None or self._paused or self._closed or not session.connected:
            return False
        await session.send_to(alias, command, actions)
        return True

    def _status(self, alias: str) -> None:
        if not self._paused and not self._closed and alias in self._aliases:
            self._on_status(alias)

    async def _watch(self, session: TendStatusSession) -> None:
        ended = asyncio.create_task(session.wait_closed())
        changed: asyncio.Task | None = None
        try:
            while self._aliases:
                changed = asyncio.create_task(self._changed.wait())
                done, _ = await asyncio.wait({ended, changed}, return_when=asyncio.FIRST_COMPLETED)
                if ended in done:
                    await ended
                    raise CannotConnect("Tend camera status connection ended")
                self._changed.clear()
                if self._aliases:
                    session.update_aliases(self._aliases)
        finally:
            tasks = [task for task in (ended, changed) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _report(self, error: MyQError) -> None:
        # A consumer callback must not strand the owned socket or spawn retries.
        with suppress(Exception):
            await self._on_error(error)

    async def _run(self) -> None:
        delay = MIN_RECONNECT_SECONDS
        try:
            while not self._paused and not self._closed:
                if not self._aliases:
                    self._changed.clear()
                    await self._changed.wait()
                    continue
                session = None
                retry_after = 0.0
                try:
                    token = await self.client.auth.access_token()
                    # Inventory may disappear while token refresh is pending.
                    if not self._aliases:
                        continue
                    session = self._session = TendStatusSession(self._aliases, self._status)
                    await session.subscribe(token)
                    await self._watch(session)
                except asyncio.CancelledError:
                    raise
                except AuthenticationError as error:
                    self._auth_failed = True
                    await self._report(error)
                    return
                except MyQError as error:
                    if isinstance(error, RateLimited):
                        retry_after = error.retry_after
                    elif isinstance(error, AccessDenied):
                        # Session-level refusal (kick-out, repeated NAKs, refused login) is not a
                        # credential failure: back off to the ceiling, then reconnect.
                        retry_after = MAX_RECONNECT_SECONDS
                    await self._report(error)
                except Exception:
                    await self._report(CannotConnect("Tend camera status connection failed"))
                finally:
                    if session is not None:
                        await session.close()
                        if self._session is session:
                            self._session = None
                if not self._aliases:
                    continue
                # Never reauthenticate in a hot loop or replay a media/control request.
                await asyncio.sleep(max(delay, retry_after))
                delay = min(delay * 2, MAX_RECONNECT_SECONDS)
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

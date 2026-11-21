"""Per-key mutual exclusion that refuses overlap instead of queuing it."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .errors import CommandRejected


class ExclusiveLocks(dict[str, asyncio.Lock]):
    """A second operation on a busy key is rejected, never run after the first."""

    @asynccontextmanager
    async def hold(self, key: str, message: str) -> AsyncIterator[None]:
        lock = self.get(key)
        if lock is None:
            lock = self[key] = asyncio.Lock()
        if lock.locked():
            raise CommandRejected(message)
        async with lock:
            yield

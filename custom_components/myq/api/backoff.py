"""Monotonic cooldowns scoped to an origin, never credentials or request paths."""

import math
import time
from urllib.parse import urlsplit

from .errors import RateLimited

# Without a Retry-After, each further 429 from the same origin doubles the wait.
BASE_DELAY = 60
MAX_STREAK_DELAY = 3600


class ServiceBackoff:
    def __init__(self) -> None:
        self._until: dict[tuple[str, str, int | None], tuple[float, bool]] = {}
        self._streak: dict[tuple[str, str, int | None], int] = {}

    @staticmethod
    def _key(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
        )

    def record(self, url: str, error: RateLimited) -> None:
        # One 429 passes through both the transport and Client._send; count it once,
        # so the streak doubles the wait per 429, not per layer that sees it.
        if error.recorded:
            return
        error.recorded = True
        delay = error.retry_after
        if type(delay) not in (float, int) or not math.isfinite(delay) or delay <= 0:
            delay = BASE_DELAY
        key = self._key(url)
        if not error.server_retry_after:
            streak = self._streak[key] = self._streak.get(key, 0) + 1
            delay = max(delay, min(BASE_DELAY * 2 ** (streak - 1), MAX_STREAK_DELAY))
        until = time.monotonic() + min(delay, 86400)
        if until >= self._until.get(key, (0, False))[0]:
            self._until[key] = until, error.server_retry_after

    def succeeded(self, url: str) -> None:
        """A response other than 429 ends that origin's streak."""
        self._streak.pop(self._key(url), None)

    def check(self, url: str) -> None:
        key = self._key(url)
        until, server = self._until.get(key, (0, False))
        remaining = until - time.monotonic()
        if remaining > 0:
            raise RateLimited(remaining, server_retry_after=server)
        self._until.pop(key, None)

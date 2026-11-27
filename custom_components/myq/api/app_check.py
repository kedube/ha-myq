"""Short-lived App Check proof supplied by an external, authorized source.

JWT expiry is an unverified freshness hint, not an authenticity check. MyQ
validates the proof. This module does not mint Google Play Integrity tokens.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .errors import (
    AppCheckError,
    AppCheckExpired,
    AppCheckInvalid,
    AppCheckUnavailable,
    RateLimited,
)

MIN_VALIDITY = 300  # The inspected Firebase SDK refreshes inside this window.
MAX_TOKEN_BYTES = 16384


def validate_app_check_token(token: str) -> None:
    """Validate header syntax without exposing its value in errors."""
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_TOKEN_BYTES
        or not token.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in token)
    ):
        raise AppCheckInvalid()


@dataclass(frozen=True)
class AppCheckProof:
    token: str = field(repr=False)
    expires_at: float

    @classmethod
    def from_token(cls, token: str) -> AppCheckProof:
        """Read exp locally; never claim this verifies a JWT signature."""
        validate_app_check_token(token)
        try:
            parts = token.split(".")
            if len(parts) != 3 or not all(parts):
                raise ValueError
            payload = json.loads(
                base64.b64decode(
                    parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
                )
            )
            expiry = payload["exp"]
            if type(expiry) not in {int, float} or not math.isfinite(expiry):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise AppCheckInvalid() from None
        return cls(token, float(expiry))

    def check_fresh(self, min_validity: float = MIN_VALIDITY) -> None:
        validate_app_check_token(self.token)
        if not math.isfinite(self.expires_at) or self.expires_at <= time.time() + min_validity:
            raise AppCheckExpired()


class AppCheckProvider(Protocol):
    async def acquire(self) -> AppCheckProof: ...

    def invalidate(self) -> None: ...


class SuppliedAppCheckProvider:
    """A proof supplied for a single sign-in; never persisted by this provider."""

    def __init__(self, token: str) -> None:
        self._proof: AppCheckProof | None = AppCheckProof.from_token(token)

    async def acquire(self) -> AppCheckProof:
        if self._proof is None:
            raise AppCheckUnavailable()
        self._proof.check_fresh()
        return self._proof

    def invalidate(self) -> None:
        self._proof = None


class CachedAppCheckProvider:
    """Bound acquisition, serialize callers, refresh stale proof and redact failures."""

    def __init__(
        self, loader: Callable[[], Awaitable[AppCheckProof]], *, timeout: float = 30
    ) -> None:
        self._loader = loader
        self._timeout = timeout
        self._proof: AppCheckProof | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self.last_status = "unavailable"

    async def acquire(self) -> AppCheckProof:
        try:
            async with asyncio.timeout(self._timeout), self._lock:
                if self._proof is not None:
                    try:
                        self._proof.check_fresh()
                        self.last_status = "ready"
                        return self._proof
                    except AppCheckExpired:
                        self._proof = None
                generation = self._generation
                proof = await self._loader()
                if not isinstance(proof, AppCheckProof):
                    raise AppCheckInvalid()
                proof.check_fresh()
                if generation != self._generation:
                    raise AppCheckUnavailable()
                self._proof = proof
                self.last_status = "ready"
                return proof
        except RateLimited:
            self.last_status = "rate_limited"
            raise
        except AppCheckError as err:
            self.last_status = err.reason
            raise
        except asyncio.CancelledError:
            self.last_status = "cancelled"
            raise
        except Exception:
            self.last_status = "unavailable"
            raise AppCheckUnavailable() from None

    def invalidate(self) -> None:
        self._generation += 1
        self._proof = None
        self.last_status = "unavailable"


def _read_token_file(path: Path) -> AppCheckProof:
    """Bound reads and reject nonregular inputs, including FIFOs, before reading."""
    try:
        fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AppCheckInvalid()
            raw = handle.read(MAX_TOKEN_BYTES * 2 + 1)
        if len(raw) > MAX_TOKEN_BYTES * 2:
            raise AppCheckInvalid()
        text = raw.decode("utf-8").strip()
        token = json.loads(text)["token"] if text.startswith("{") else text
    except (OSError, RuntimeError):
        raise AppCheckUnavailable() from None
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise AppCheckInvalid() from None
    return AppCheckProof.from_token(token)


class FileAppCheckProvider(CachedAppCheckProvider):
    """Reload externally renewed JSON or raw JWT files when proof becomes stale."""

    def __init__(self, path: Path, *, timeout: float = 30) -> None:
        async def load() -> AppCheckProof:
            return await asyncio.to_thread(_read_token_file, path)

        super().__init__(load, timeout=timeout)

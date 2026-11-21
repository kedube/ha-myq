"""OAuth token exchange and serialized rotation, without storing passwords."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .app_check import AppCheckProof, validate_app_check_token
from .const import CLIENT_ID, IDENTITY, NATIVE_HEADERS, REDIRECT_URI, SCOPE
from .errors import (
    ApplicationVerificationError,
    AuthenticationError,
    MyQError,
    ProtocolError,
    SessionRenewalFailed,
)
from .http import HttpTransport, Response, check_status

# Protocol codes only. Never surface arbitrary server descriptions or field values.
OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
    }
)
MYQ_CODES = frozenset({"400", "401", "401.102", "401.122", "401.123", "401.125", "403.126"})
# ip0/j.f91077t: the lifetime the app assumes when a response omits expires_in.
DEFAULT_LIFETIME = 3600.0
# Saved before lifetimes were recorded: renew this long before expiry.
LEGACY_MARGIN = 60.0
# ip0/j.f91078u ends the session for these refresh answers (ip0/p.b checks error,
# then code). ft0/g1 and ft0/c1 also sign out when the refresh fails with code 400.
SESSION_ENDED_ERRORS = frozenset({"invalid_grant"})
SESSION_ENDED_CODES = frozenset({"401.102", "400"})
TOKEN_URL = IDENTITY + "/connect/token"
# ip0/s.f, ip0/k.J: the app's one-time sign-in exchange (api/{v1.0}/auth/token).
ONE_TIME_URL = IDENTITY + "/api/v1.0/auth/token"
# ip0/s.h, ip0/k.N: sign-out revocation.
REVOCATION_URL = IDENTITY + "/connect/revocation"
MAX_ONE_TIME_TOKEN = 8192

Grant = Literal["code", "refresh", "one_time"]


async def _complete_despite_cancellation[T](operation: Coroutine[Any, Any, T]) -> T:
    """Finish token I/O even when the awaiting caller is cancelled.

    A sent exchange consumes its authorization code or rotating refresh token
    server-side, and only its response carries the replacement. A caller's timeout
    (camera image, status pause, closed WebRTC offer, shutdown) must not discard it.
    The task keeps the manager lock until the new tokens are published and persisted.
    """
    task = asyncio.create_task(operation)
    task.add_done_callback(lambda done: done.cancelled() or done.exception())
    return await asyncio.shield(task)


def exchange_diagnostics(response: Response) -> dict[str, str | int | None]:
    """Retain only status, format, and recognized protocol error codes."""
    summary: dict[str, str | int | None] = {
        "http_status": response.status,
        "response_format": "empty" if not response.body else "other",
        "oauth_error": None,
        "myq_code": None,
    }
    try:
        data = response.json()
    except ProtocolError:
        content_type = response.header("Content-Type") or ""
        if content_type.lower().split(";", 1)[0].strip() == "text/html":
            summary["response_format"] = "html"
        return summary
    summary["response_format"] = "json"
    if isinstance(data, dict):
        error, code = data.get("error"), data.get("code")
        if isinstance(error, str) and error in OAUTH_ERRORS:
            summary["oauth_error"] = error
        if isinstance(code, str) and code in MYQ_CODES:
            summary["myq_code"] = code
    return summary


def _lifetime(value: Any) -> float:
    """expires_in, or the app's one-hour default when it is missing or unusable."""
    if type(value) is bool:
        return DEFAULT_LIFETIME
    try:
        lifetime = float(value)
    except (TypeError, ValueError):
        return DEFAULT_LIFETIME
    if not math.isfinite(lifetime) or lifetime <= 0:
        return DEFAULT_LIFETIME
    return lifetime


def validate_one_time_token(token: str) -> None:
    """Header-safe syntax only; MyQ decides whether the token is valid."""
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_ONE_TIME_TOKEN
        or not token.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in token)
    ):
        raise ProtocolError("The one-time sign-in token is malformed")


@dataclass(frozen=True)
class Tokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    # Seconds the access token was issued for; 0 for sessions saved before this was kept.
    lifetime: float = 0.0

    @classmethod
    def from_response(cls, data: Any, previous_refresh: str = "") -> Tokens:
        """Accept what the app accepts (ap0/AuthLoginDto), so a rotation is never lost.

        A missing expires_in means one hour (ip0/j.I0). token_type is ignored: the app
        always sends "Bearer" (ip0/r). An omitted refresh token keeps the current one.
        """
        if not isinstance(data, dict):
            raise ProtocolError("Invalid OAuth response")
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            refresh = previous_refresh
        if not isinstance(access, str) or not access or not refresh:
            raise ProtocolError("Invalid OAuth token response")
        lifetime = _lifetime(data.get("expires_in"))
        return cls(access, refresh, time.time() + lifetime, lifetime)

    @property
    def renew_at(self) -> float:
        """ip0/j.z0: treat the token as expired after max(95%, lifetime - 5 minutes)."""
        if self.lifetime > 0 and math.isfinite(self.lifetime):
            return self.expires_at - min(0.05 * self.lifetime, 300.0)
        return self.expires_at - LEGACY_MARGIN

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TokenManager:
    def __init__(
        self,
        http: HttpTransport,
        tokens: Tokens | None = None,
        on_update: Callable[[Tokens], Awaitable[None]] | None = None,
    ) -> None:
        self.http = http
        self.tokens = tokens
        self.on_update = on_update
        self.last_exchange: dict[str, str | int | bool | None] | None = None
        self._lock = asyncio.Lock()

    async def _exchange(
        self,
        form: dict[str, str],
        headers: dict[str, str],
        *,
        grant: Grant,
        url: str = TOKEN_URL,
    ) -> Tokens:
        self.last_exchange = {
            "app_check_supplied": bool(headers.get("Firebase-AppCheck-Token")),
            "http_status": None,
        }
        response = await self.http.request(
            "POST", url, data=form, headers={**NATIVE_HEADERS, **headers}
        )
        self.last_exchange.update(exchange_diagnostics(response))
        oauth_error, code = self.last_exchange["oauth_error"], self.last_exchange["myq_code"]
        if response.status in {400, 401, 403} and code == "401.122":
            raise ApplicationVerificationError()
        if response.status in (400, 401):
            self._reject(grant, oauth_error, code)
        check_status(response)
        previous = self.tokens.refresh_token if grant == "refresh" and self.tokens else ""
        tokens = Tokens.from_response(response.json(), previous)
        # Publish the rotation before any other coroutine can use the old token.
        self.tokens = tokens
        if self.on_update:
            await self.on_update(tokens)
        return tokens

    @staticmethod
    def _reject(grant: Grant, oauth_error: Any, code: Any) -> None:
        if grant == "refresh":
            # The app signs out only for these; any other refusal keeps the session
            # and is retried later (ip0/j.J0 -> g0, ft0/g1, ft0/c1).
            if oauth_error in SESSION_ENDED_ERRORS or code in SESSION_ENDED_CODES:
                raise AuthenticationError(
                    f"MyQ ended this session ({oauth_error or code}); sign in again"
                )
            raise SessionRenewalFailed()
        if grant == "one_time":
            raise AuthenticationError(
                "MyQ did not accept the one-time sign-in link; it may be used or expired"
            )
        if oauth_error == "invalid_client":
            raise AuthenticationError("MyQ rejected OAuth client authentication (invalid_client)")
        if oauth_error == "invalid_grant":
            raise AuthenticationError("MyQ rejected the authorization grant (invalid_grant)")
        raise AuthenticationError("MyQ rejected the authorization or refresh token")

    async def exchange_code(
        self,
        code: str,
        verifier: str,
        app_check_token: str | None = None,
        *,
        proof: AppCheckProof | None = None,
        before_send: Callable[[], None] | None = None,
    ) -> Tokens:
        if proof is not None:
            if app_check_token is not None:
                raise ProtocolError("Choose one App Check proof source")
            app_check_token = proof.token
        if app_check_token is not None:
            validate_app_check_token(app_check_token)
        headers = {"Firebase-AppCheck-Token": app_check_token} if app_check_token else {}

        async def exchange() -> Tokens:
            async with self._lock:
                if proof is not None:
                    proof.check_fresh()
                if before_send is not None:
                    before_send()
                return await self._exchange(
                    {
                        "client_id": CLIENT_ID,
                        "scope": SCOPE,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": REDIRECT_URI,
                        "code_verifier": verifier,
                    },
                    headers,
                    grant="code",
                )

        return await _complete_despite_cancellation(exchange())

    async def exchange_one_time(self, token: str) -> Tokens:
        """Swap a one-time sign-in token for a session, as the app's link handler does.

        gp0/f.h -> ip0/j.g stores the token as the access token, so the header
        interceptor (ft0/i0) sends it as a Bearer credential; the form carries only
        client_id. This request has no App Check header. It is sent once, never
        replayed, because the server consumes the token.
        """
        validate_one_time_token(token)

        async def exchange() -> Tokens:
            async with self._lock:
                return await self._exchange(
                    {"client_id": CLIENT_ID},
                    {"Authorization": "Bearer " + token},
                    grant="one_time",
                    url=ONE_TIME_URL,
                )

        return await _complete_despite_cancellation(exchange())

    async def access_token(self, rejected_token: str | None = None) -> str:
        return await _complete_despite_cancellation(self._access_token(rejected_token))

    async def _access_token(self, rejected_token: str | None) -> str:
        async with self._lock:
            tokens = self.tokens
            if tokens is None:
                raise AuthenticationError("Sign in to MyQ first")
            if tokens.renew_at > time.time() and tokens.access_token != rejected_token:
                return tokens.access_token
            # A concurrent request may already have rotated the rejected token.
            headers = {"IsRefresh": "true"}
            if tokens.access_token:
                # ft0/i0 adds the stored access token to every request it sees,
                # including the refresh that MyQOAuthInterceptor (ft0/t0) builds.
                headers["Authorization"] = "Bearer " + tokens.access_token
            tokens = await self._exchange(
                {
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                },
                headers,
                grant="refresh",
            )
            return tokens.access_token

    async def revoke(self) -> bool:
        """End this session at MyQ, as the app's sign-out does (gp0/f.d -> ip0/k.N).

        The app's revocation is an authenticated request, so it carries a current
        access token when one can be had; the refresh token alone identifies the
        session. Returns whether MyQ confirmed it. Never retried.
        """
        if self.tokens is None or not self.tokens.refresh_token:
            return False
        headers = dict(NATIVE_HEADERS)
        with suppress(MyQError):
            headers["Authorization"] = "Bearer " + await self.access_token()

        async def send() -> bool:
            async with self._lock:
                tokens = self.tokens
                if tokens is None or not tokens.refresh_token:
                    return False
                response = await self.http.request(
                    "POST",
                    REVOCATION_URL,
                    data={
                        "client_id": CLIENT_ID,
                        "token_type_hint": "refresh_token",
                        "token": tokens.refresh_token,
                    },
                    headers=headers,
                )
                if not 200 <= response.status < 300:
                    return False
                self.tokens = None
                return True

        return await _complete_despite_cancellation(send())

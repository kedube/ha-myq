"""Bounded HTTP I/O with TLS validation and no redirect or mutation replay."""

import json
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import aiohttp

from .backoff import ServiceBackoff
from .const import MAX_RESPONSE, TIMEOUT, USER_AGENT
from .errors import (
    AccessDenied,
    BrowserVerificationRequired,
    CannotConnect,
    ProtocolError,
    RateLimited,
    RequestUnauthorized,
)
from .native import browser_header, uses_native_profile

# ft0/c1.a: body codes that mean "renew the access token and send again".
RENEWABLE_CODES = frozenset({"401.102", "401"})


@dataclass(repr=False)
class Response:
    status: int
    headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("MyQ returned invalid JSON") from None

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup; transports flatten their header multidicts."""
        name = name.lower()
        return next((value for key, value in self.headers.items() if key.lower() == name), None)


class HttpTransport(Protocol):
    async def request(self, method: str, url: str, **kwargs: Any) -> Response: ...


def retry_seconds(value: str | None) -> float:
    if value is None:
        return 60
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError, AttributeError):
            seconds = 60
    if not math.isfinite(seconds) or seconds < 1:
        return 60
    # A long wait stays long (clamped to a day); only nonsense falls back to a minute.
    return min(seconds, 86400)


def is_browser_challenge(headers: Mapping[str, str]) -> bool:
    return any(
        key.lower() == "cf-mitigated" and value.strip().lower() == "challenge"
        for key, value in headers.items()
    )


def _single_attempt_middleware() -> aiohttp.ClientMiddlewareType:
    """Prevent a second send, including aiohttp's implicit retry of a PUT."""
    attempted = False

    async def send_once(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        nonlocal attempted
        if attempted:
            raise CannotConnect("MyQ request outcome is unknown; automatic replay was blocked")
        attempted = True
        return await handler(request)

    return send_once


def _native_headers_middleware(*, explicit_accept: bool) -> aiohttp.ClientMiddlewareType:
    """Drop browser/session defaults after aiohttp has merged headers and cookies."""

    async def native_headers(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        for name in tuple(request.headers):
            if browser_header(name) or (name.lower() == "accept" and not explicit_accept):
                request.headers.popall(name, None)
        return await handler(request)

    return native_headers


class Http:
    """Share the injected connection pool, keeping native cookies out of sign-in."""

    def __init__(
        self, session: aiohttp.ClientSession, backoff: ServiceBackoff | None = None
    ) -> None:
        self.session = session
        # A store passed in outlives this transport, e.g. across setup retries.
        self.backoff = backoff if backoff is not None else ServiceBackoff()

    @asynccontextmanager
    async def _request_session(self, native: bool) -> AsyncIterator[aiohttp.ClientSession]:
        if not native:
            yield self.session
            return
        # Sessions are lightweight; the caller's connector retains pooled sockets.
        # NoCookies in the Android app means neither sending nor saving cookies.
        # A separate session avoids changing HA's shared headers/cookie jar.
        async with aiohttp.ClientSession(
            connector=self.session.connector,
            connector_owner=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=self.session.trust_env,
        ) as session:
            yield session

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.backoff.check(url)
        headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
        native = uses_native_profile(headers)
        timeout = kwargs.pop("timeout", None) or TIMEOUT
        single_send = method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}
        if native or single_send:
            middlewares = kwargs.pop("middlewares", None)
            if middlewares is None:
                # aiohttp has no public middleware getter. Preserve HA's existing
                # middleware when supplying a per-request chain; never mutate it.
                middlewares = getattr(self.session, "_middlewares", None)
                if middlewares is None:
                    raise ProtocolError("Cannot preserve HTTP middleware for a single-send request")
            # Innermost placement also blocks retries by an outer middleware.
            if native:
                middlewares = (
                    *middlewares,
                    _native_headers_middleware(
                        explicit_accept=any(name.lower() == "accept" for name in headers)
                    ),
                )
                kwargs["skip_auto_headers"] = {
                    *(kwargs.pop("skip_auto_headers", None) or ()),
                    "Accept",
                }
            if single_send:
                middlewares = (*middlewares, _single_attempt_middleware())
            kwargs["middlewares"] = middlewares
        try:
            async with (
                self._request_session(native) as session,
                session.request(
                    method,
                    url,
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    **kwargs,
                ) as response,
            ):
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE:
                        raise ProtocolError("MyQ response exceeded size limit")
                result = Response(response.status, dict(response.headers), bytes(chunks))
                if is_browser_challenge(response.headers):
                    raise BrowserVerificationRequired()
                if response.status == 429:
                    delay = retry_seconds(response.headers.get("Retry-After"))
                    error = RateLimited(delay, server_retry_after="Retry-After" in response.headers)
                    self.backoff.record(url, error)
                    raise error
                self.backoff.succeeded(url)
                return result
        except aiohttp.ClientConnectorCertificateError:
            raise CannotConnect("TLS certificate verification failed for MyQ") from None
        except aiohttp.ClientConnectorDNSError:
            raise CannotConnect("DNS lookup failed for the MyQ service") from None
        except aiohttp.ClientSSLError:
            raise CannotConnect("TLS negotiation failed with MyQ") from None
        except TimeoutError:
            raise CannotConnect(
                f"MyQ did not respond within the {timeout:g}-second network timeout"
            ) from None
        except aiohttp.ClientConnectorError as err:
            raise CannotConnect(
                f"Network connection to MyQ failed (OS error {err.os_error.errno})"
            ) from None
        except aiohttp.ClientError as err:
            # aiohttp exception messages may contain credential-bearing URLs.
            raise CannotConnect(f"MyQ connection failed ({type(err).__name__})") from None


def renewable(response: Response) -> bool:
    """Whether a 401 asks for a renewed access token, as the app's authenticator reads it.

    ft0/c1 renews for body code 401.102 or 401, or a 401 whose body has no code.
    Any other coded 401 fails that request without renewing.
    """
    if response.status != 401:
        return False
    try:
        data = response.json()
    except ProtocolError:
        return True
    code = data.get("code") if isinstance(data, dict) else None
    if type(code) is int:
        code = str(code)
    if not isinstance(code, str) or not code:
        return True
    return code in RENEWABLE_CODES


def check_status(response: Response) -> None:
    if is_browser_challenge(response.headers):
        raise BrowserVerificationRequired()
    if response.status == 429:
        retry_after = response.header("Retry-After")
        raise RateLimited(retry_seconds(retry_after), server_retry_after=retry_after is not None)
    if response.status == 401:
        # One rejected request does not end the session (ft0/c1); the token
        # endpoint alone decides that, in tokens.TokenManager.
        raise RequestUnauthorized()
    if response.status == 403:
        raise AccessDenied("MyQ denied access; permissions or app verification may be required")
    if response.status >= 500:
        raise CannotConnect("MyQ service is temporarily unavailable")
    if not 200 <= response.status < 300:
        raise ProtocolError(f"Unexpected MyQ HTTP status {response.status}")

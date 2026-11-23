"""Separate hosted-browser and measured Android native HTTP transports.

Keep browser navigation and native API sessions separate. This HTTP client does
not execute JavaScript, solve browser challenges, or produce app attestation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import certifi
from curl_cffi import CurlECode, CurlHttpVersion, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from .android_tls import ANDROID_EXTRA_FP, ANDROID_HTTP2, ANDROID_JA3
from .backoff import ServiceBackoff
from .const import HOSTED_USER_AGENT, MAX_RESPONSE, TIMEOUT
from .errors import BrowserVerificationRequired, CannotConnect, ProtocolError, RateLimited
from .http import Response, is_browser_challenge, retry_seconds
from .native import browser_header, uses_native_profile

# Methods a transport may send again without changing anything at MyQ.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CurlHttp:
    """Own verified TLS pools; the HA adapter uses the native pools only.

    A pool is created when it is first used: Home Assistant never uses the browser
    one, and closing a pool that was never used would start it only to stop it.
    """

    def __init__(self, profile: str = "chrome_android") -> None:
        if profile not in {"chrome_android", "chrome"}:
            raise ValueError("Choose chrome_android or chrome")
        # AsyncSession accepts these aliases and resolves them itself. Its
        # internal resolver was renamed in curl_cffi 0.17; importing it would
        # prevent HA from loading the config flow when that version is present.
        self.profile = profile
        self._pools: dict[str, AsyncSession] = {}
        self.backoff = ServiceBackoff()
        self.last_status: int | None = None
        self.browser_verification_seen = False
        self._closed = False

    def _pool(self, kind: str) -> AsyncSession:
        pool = self._pools.get(kind)
        if pool is None:
            if kind == "browser":
                pool = AsyncSession(
                    verify=certifi.where(),
                    trust_env=False,
                    # Explicitly disable libcurl's environment-proxy fallback too.
                    proxy="",
                    impersonate=self.profile,
                    default_headers=True,
                )
            else:
                # Android uses HTTP/1 for cleartext, not an h2c Upgrade request.
                options: dict[CurlOpt, int] = {CurlOpt.HTTP_VERSION: CurlHttpVersion.V2TLS}
                if kind == "native_single_send":
                    options[CurlOpt.FRESH_CONNECT] = 1
                    options[CurlOpt.FORBID_REUSE] = 1
                pool = AsyncSession(
                    verify=certifi.where(),
                    trust_env=False,
                    proxy="",
                    default_headers=False,
                    # curl manages padding extension 21 itself; the emitted Hello was
                    # compared with the captured profile including that extension.
                    ja3=ANDROID_JA3.replace("-21,", ","),
                    akamai=ANDROID_HTTP2,
                    extra_fp=ANDROID_EXTRA_FP,
                    curl_options=options,
                )
            self._pools[kind] = pool
        return pool

    @property
    def session(self) -> AsyncSession:
        """The browser-profile pool, with cookies (hosted sign-in pages)."""
        return self._pool("browser")

    @property
    def native_session(self) -> AsyncSession:
        # Native OkHttp uses NoCookies. A separate pool also prevents the browser
        # TLS/HTTP profile from being retained on a reused native connection.
        return self._pool("native")

    @property
    def native_single_send(self) -> AsyncSession:
        """The native profile on a connection of its own, for anything but a read.

        libcurl sends a request again, whatever its method, when a reused connection
        fails before the first response byte. MyQ may already have acted on it, so a
        door command or a token renewal would be delivered twice with no error. A
        connection that is never reused is exempt: its failure is reported instead.
        """
        return self._pool("native_single_send")

    async def __aenter__(self) -> CurlHttp:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pools, self._pools = list(self._pools.values()), {}
        errors = await asyncio.gather(*(pool.close() for pool in pools), return_exceptions=True)
        for error in errors:
            if isinstance(error, BaseException):
                raise error

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if self._closed:
            raise CannotConnect("MyQ HTTP transport is closed")
        self.backoff.check(url)
        headers = dict(kwargs.pop("headers", {}))
        native = uses_native_profile(headers)
        if not native:
            session = self.session
        elif method.upper() in SAFE_METHODS:
            session = self.native_session
        else:
            session = self.native_single_send
        timeout = kwargs.pop("timeout", None) or TIMEOUT
        if native:
            headers = {name: value for name, value in headers.items() if not browser_header(name)}
            if not any(name.lower() == "accept" for name in headers):
                headers["Accept"] = None  # Suppress libcurl's default */*.
            # OkHttp's BridgeInterceptor appends this after application metadata.
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = "gzip"
            kwargs.pop("cookies", None)
            kwargs.update(accept_encoding="gzip", discard_cookies=True, default_headers=False)
        elif any(k.lower() == "sec-fetch-mode" and v == "cors" for k, v in headers.items()):
            # The captured SSO AJAX lookup is not a user-activated document
            # navigation. Suppress these defaults from the browser profile.
            headers["Sec-Fetch-User"] = None
            headers["Upgrade-Insecure-Requests"] = None
        if not native and headers.get("User-Agent") == HOSTED_USER_AGENT:
            # Login owns its hosted header set; a diagnostic TLS preset must not
            # inject a different browser's navigation defaults over that set.
            kwargs["default_headers"] = False
        chunks = bytearray()
        oversized = False

        def receive(chunk: bytes) -> int:
            nonlocal oversized
            if len(chunks) + len(chunk) > MAX_RESPONSE:
                oversized = True
                # Return the C API's abort marker; raising in a CFFI callback
                # could print a traceback rather than stop the transfer cleanly.
                return CURL_WRITEFUNC_ERROR
            chunks.extend(chunk)
            return len(chunk)

        try:
            response = await session.request(
                method,
                url,
                # Browser identity stays on navigation; native metadata is explicit.
                allow_redirects=False,
                timeout=timeout,
                content_callback=receive,
                headers=headers,
                **kwargs,
            )
        except RequestException as error:
            if oversized:
                raise ProtocolError("MyQ response exceeded size limit") from None
            messages = {
                CurlECode.PEER_FAILED_VERIFICATION: "TLS certificate verification failed for MyQ",
                CurlECode.SSL_CACERT_BADFILE: "Could not load the MyQ TLS certificate bundle",
                CurlECode.SSL_CONNECT_ERROR: "TLS negotiation failed with MyQ",
                CurlECode.COULDNT_RESOLVE_HOST: "DNS lookup failed for the MyQ service",
                CurlECode.COULDNT_CONNECT: "Network connection to MyQ failed",
                CurlECode.OPERATION_TIMEDOUT: (
                    f"MyQ did not respond within the {timeout:g}-second network timeout"
                ),
            }
            # libcurl errors may contain URLs, cookies, and form values.
            raise CannotConnect(
                messages.get(error.code, f"MyQ connection failed (curl code {int(error.code)})")
            ) from None
        if oversized:
            raise ProtocolError("MyQ response exceeded size limit")
        if response.status_code == 0:
            # Closing a pool completes its requests still in flight with an empty
            # answer. Nothing was received: the outcome of that request is unknown.
            raise CannotConnect("MyQ HTTP transport closed before a response arrived")
        self.last_status = response.status_code
        result = Response(response.status_code, dict(response.headers), bytes(chunks))
        if is_browser_challenge(result.headers):
            self.browser_verification_seen = True
            raise BrowserVerificationRequired()
        if result.status == 429:
            retry_after = result.header("Retry-After")
            delay = retry_seconds(retry_after)
            limited = RateLimited(delay, server_retry_after=retry_after is not None)
            self.backoff.record(url, limited)
            raise limited
        # A non-429 response ends this native origin's streak, as Http.request does;
        # otherwise a native host's escalating cooldown never resets after a success.
        self.backoff.succeeded(url)
        return result

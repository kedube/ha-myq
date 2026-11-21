"""Optional probe transport; Home Assistant does not import this module.

Keep one browser profile and cookie jar throughout the flow. This HTTP client
does not execute JavaScript, solve browser challenges, or produce app attestation.
"""

from __future__ import annotations

from typing import Any

import certifi
from curl_cffi import CurlECode
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.impersonate import normalize_browser_type

from .backoff import ServiceBackoff
from .const import MAX_RESPONSE, TIMEOUT
from .errors import BrowserVerificationRequired, CannotConnect, ProtocolError, RateLimited
from .http import Response, is_browser_challenge, retry_seconds


class CurlHttp:
    """Own a short-lived, verified TLS session for the standalone probe."""

    def __init__(self, profile: str = "chrome_android") -> None:
        if profile not in {"chrome_android", "chrome"}:
            raise ValueError("Choose chrome_android or chrome")
        self.profile = normalize_browser_type(profile)
        self.session = AsyncSession(
            verify=certifi.where(),
            trust_env=False,
            # Explicitly disable libcurl's environment-proxy fallback too.
            proxy="",
            impersonate=self.profile,
            default_headers=True,
        )
        self.backoff = ServiceBackoff()
        self.last_status: int | None = None
        self.browser_verification_seen = False

    async def __aenter__(self) -> CurlHttp:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.session.close()

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.backoff.check(url)
        headers = dict(kwargs.pop("headers", {}))
        timeout = kwargs.pop("timeout", None) or TIMEOUT
        if any(k.lower() == "sec-fetch-mode" and v == "cors" for k, v in headers.items()):
            # The captured SSO AJAX lookup is not a user-activated document
            # navigation. Suppress these defaults from the browser profile.
            headers["Sec-Fetch-User"] = None
            headers["Upgrade-Insecure-Requests"] = None
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
            response = await self.session.request(
                method,
                url,
                # Preserve the profile's User-Agent and client hints. Protocol
                # headers (Origin, CSRF, Authorization) are added by callers.
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
                    "MyQ did not respond within the 30-second network timeout"
                ),
            }
            # libcurl errors may contain URLs, cookies, and form values.
            raise CannotConnect(
                messages.get(error.code, f"MyQ connection failed (curl code {int(error.code)})")
            ) from None
        if oversized:
            raise ProtocolError("MyQ response exceeded size limit")
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
        return result

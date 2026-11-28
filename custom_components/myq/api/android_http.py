"""HA transport: Android TLS/HTTP2 for native MyQ calls, Chrome's for MyQ's hosted
sign-in pages, aiohttp for other services."""

from typing import Any
from urllib.parse import urlsplit

from .const import IDENTITY
from .curl_http import CurlHttp
from .errors import CannotConnect
from .http import Http, Response
from .native import uses_native_profile

_IDENTITY = urlsplit(IDENTITY)


def hosted_page(url: str) -> bool:
    """A page on MyQ's sign-in service, which Cloudflare checks for a real browser."""
    parts = urlsplit(url)
    return parts.scheme == _IDENTITY.scheme and parts.netloc == _IDENTITY.netloc


class AndroidHttp(Http):
    """Keep native connections and cookies separate from hosted sign-in pages.

    Hosted pages use the Chrome profile's pool, whose cookies last for this
    transport, so each sign-in attempt (a new client) starts with none.
    """

    _native: CurlHttp | None = None
    _closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if self._closed:
            raise CannotConnect("MyQ HTTP transport is closed")
        if not uses_native_profile(kwargs.get("headers", {})) and not hosted_page(url):
            return await super().request(method, url, **kwargs)
        if self._native is None:
            self._native = CurlHttp()
            self._native.backoff = self.backoff
        return await self._native.request(method, url, **kwargs)

    async def close(self) -> None:
        self._closed = True
        native, self._native = self._native, None
        if native is not None:
            await native.close()

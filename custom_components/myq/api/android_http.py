"""HA transport: Android TLS/HTTP2 for native MyQ calls, aiohttp for other services."""

from typing import Any

from .curl_http import CurlHttp
from .errors import CannotConnect
from .http import Http, Response
from .native import uses_native_profile


class AndroidHttp(Http):
    """Keep native connections and cookies separate from hosted sign-in pages."""

    _native: CurlHttp | None = None
    _closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if self._closed:
            raise CannotConnect("MyQ HTTP transport is closed")
        if not uses_native_profile(kwargs.get("headers", {})):
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

"""Interactive browser transport for the local probe; not run inside HA.

MyQ's app uses Android Custom Tabs for unified sign-in. Here the user completes
the real hosted pages in an isolated desktop Chrome window. We observe only
navigation status and the OAuth return; no password/OTP form scraping, browser
identity overrides, TLS bypass, storage export, or attestation generation.
"""

import asyncio
import base64
import time
from collections.abc import Callable
from urllib.parse import urljoin

from .auth import identity_url
from .const import IDENTITY
from .errors import (
    AccessDenied,
    AuthenticationError,
    CannotConnect,
    MyQError,
    ProtocolError,
    RateLimited,
    UnsupportedAuthStep,
)
from .handoff import BrowserAuthorization
from .http import retry_seconds
from .oauth import OAuthSession, is_callback


class BrowserLogin:
    def __init__(
        self,
        progress: Callable[[str], None] | None = None,
        *,
        oauth: OAuthSession | BrowserAuthorization | None = None,
    ) -> None:
        self.oauth = oauth if oauth is not None else OAuthSession()
        self.progress = progress or (lambda message: None)
        self.last_status: int | None = None
        self.browser_verification_seen = False

    async def authorize(
        self, *, connection_check: bool = False, channel: str = "chrome"
    ) -> str | None:
        try:
            from playwright.async_api import Error, async_playwright
        except ImportError:
            raise UnsupportedAuthStep(
                "Browser login needs Playwright: run .venv/bin/python -m pip install '.[browser]'"
            ) from None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel=channel, headless=False)
                try:
                    context = await browser.new_context(accept_downloads=False)
                    page = await context.new_page()
                    self.progress(
                        "Complete MyQ email/password and MFA in the opened browser window"
                    )
                    return await self._watch(page, connection_check=connection_check)
                finally:
                    await browser.close()
        except Error:
            raise CannotConnect(
                "The Chrome sign-in window could not run; check that Chrome is installed"
            ) from None

    async def _watch(self, page, *, connection_check: bool = False) -> str | None:
        """Observe a browser page without reading credential inputs or page bodies."""
        from playwright.async_api import Error
        from playwright.async_api import TimeoutError as BrowserTimeout

        result = asyncio.get_running_loop().create_future()
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.enable")
        frame_id = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]

        async def paused_response(event: dict) -> None:
            """Capture the server redirect before Chrome dispatches the app URI."""
            request_id = event["requestId"]
            try:
                identity_url(event["request"]["url"])
                headers = {h["name"].lower(): h["value"] for h in event.get("responseHeaders", [])}
                target = urljoin(event["request"]["url"], headers.get("location", ""))
                if (
                    event.get("frameId") == frame_id
                    and event.get("responseStatusCode") in {301, 302, 303, 307, 308}
                    and is_callback(target)
                ):
                    # Replace only the final app redirect with a local completion
                    # page. The callback itself is never sent to another origin.
                    await cdp.send(
                        "Fetch.fulfillRequest",
                        {
                            "requestId": request_id,
                            "responseCode": 200,
                            "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                            "body": base64.b64encode(
                                b"<p>MyQ returned to the local probe."
                                b" You can close this window.</p>"
                            ).decode("ascii"),
                        },
                    )
                    accept_callback(target)
                    return
            except ProtocolError:
                pass
            except Error:
                fail(
                    CannotConnect("Browser disconnected while handling the authorization callback")
                )
                return
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": request_id})
            except Error:
                if not result.done():
                    fail(CannotConnect("Browser disconnected during sign-in navigation"))

        def fail(error: MyQError) -> None:
            if not result.done():
                result.set_exception(error)

        def accept_callback(url: str) -> None:
            if result.done():
                return
            try:
                self.oauth.callback_code(url)
            except MyQError as error:
                fail(error)
            else:
                self.progress("Received and validated the MyQ authorization callback")
                result.set_result(url)

        def requested_navigation(event: dict) -> None:
            url = event.get("url", "")
            if event.get("frameId") != frame_id or not is_callback(url):
                return
            try:
                identity_url(page.url)
            except ProtocolError:
                fail(ProtocolError("Authorization callback did not originate from MyQ"))
                return
            accept_callback(url)

        async def response_received(response) -> None:
            if result.done():
                return
            try:
                # The browser may use other origins for page assets or SSO.
                # Only MyQ's own top-frame response can issue our callback.
                identity_url(response.url)
            except ProtocolError:
                return
            try:
                if response.frame != page.main_frame:
                    return
                navigation = response.request.is_navigation_request()
                if navigation:
                    self.last_status = response.status
                    self.progress(f"MyQ browser navigation returned HTTP {response.status}")
                if await response.header_value("cf-mitigated") == "challenge":
                    if not self.browser_verification_seen:
                        self.progress(
                            "MyQ requires browser verification. Let the page finish or complete "
                            "the verification prompt in Chrome; this is not a rate-limit timer."
                        )
                    self.browser_verification_seen = True
                    # The app's browser is allowed to render/run this interstitial.
                    # Do not close it, scrape it, or attempt to solve it ourselves.
                    return
                if response.status == 429:
                    retry = await response.header_value("retry-after")
                    fail(RateLimited(retry_seconds(retry), server_retry_after=retry is not None))
                    return
                if navigation and response.status == 403:
                    fail(AccessDenied("MyQ denied the browser sign-in page"))
                    return
                if navigation and response.status in {301, 302, 303, 307, 308}:
                    location = await response.header_value("location")
                    if location and is_callback(target := urljoin(response.url, location)):
                        accept_callback(target)
                elif connection_check and navigation and 200 <= response.status < 300:
                    if not result.done():
                        result.set_result(None)
            except Error:
                if not result.done():
                    fail(CannotConnect("Browser disconnected while reading the sign-in response"))

        def closed() -> None:
            fail(AuthenticationError("The sign-in browser window was closed before completion"))

        page.on("response", response_received)
        page.on("close", closed)
        cdp.on("Page.frameRequestedNavigation", requested_navigation)
        cdp.on("Fetch.requestPaused", paused_response)
        try:
            await cdp.send(
                "Fetch.enable",
                {
                    "patterns": [
                        {
                            "urlPattern": IDENTITY + "/*",
                            "resourceType": "Document",
                            "requestStage": "Response",
                        }
                    ]
                },
            )
            try:
                response = await page.goto(
                    self.oauth.authorize_url, wait_until="domcontentloaded", timeout=30000
                )
            except BrowserTimeout:
                self.progress("The sign-in page is still loading; inspect the browser window")
                response = None
            except Error:
                # A custom-scheme return can end navigation with ERR_ABORTED.
                # The validated callback or a specific HTTP error takes priority.
                if not result.done():
                    raise CannotConnect("Could not load MyQ in the browser") from None
                response = None
            if result.done():
                return await result
            if connection_check:
                # Challenge pages may be 403 or 429. Wait for an unchallenged
                # page instead of closing the browser before its script runs.
                if response is not None:
                    await response_received(response)
                if result.done():
                    return await result
            self.progress("Waiting for you to finish sign-in in the browser (up to 10 minutes)")
            try:
                return await asyncio.wait_for(result, max(0, self.oauth.expires - time.monotonic()))
            except TimeoutError:
                raise AuthenticationError("Browser sign-in expired; start a new attempt") from None
        finally:
            page.remove_listener("response", response_received)
            page.remove_listener("close", closed)
            cdp.remove_listener("Page.frameRequestedNavigation", requested_navigation)
            cdp.remove_listener("Fetch.requestPaused", paused_response)
            if result.done() and not result.cancelled():
                result.exception()  # Retrieve exceptions even if navigation failed first.
            else:
                result.cancel()
            try:
                await cdp.send("Fetch.disable")
                await cdp.detach()
            except Error:
                pass

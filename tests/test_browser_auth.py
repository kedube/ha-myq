"""Optional real-Chrome tests. All page requests use synthetic, intercepted data.

Run with MYQ_BROWSER_TESTS=1. No requests are sent to the actual MyQ service.
"""

import asyncio
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.myq.api.browser import BrowserLogin
from custom_components.myq.api.const import IDENTITY
from custom_components.myq.api.errors import AuthenticationError, RateLimited
from custom_components.myq.api.handoff import BrowserAuthorization, create_request
from custom_components.myq.api.oauth import OAuthSession

pytestmark = [
    pytest.mark.enable_socket,
    pytest.mark.skipif(os.getenv("MYQ_BROWSER_TESTS") != "1", reason="Opt-in real browser test"),
]


@pytest.fixture
async def page():
    api = pytest.importorskip("playwright.async_api")
    async with api.async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        yield page
        await browser.close()


async def test_real_browser_executes_javascript_and_completes_mfa(page):
    login = BrowserLogin()
    seen = []
    callback = f"com.myqops://android?code=synthetic-code&state={login.oauth.state}"
    password_page = """<script>document.cookie='js_executed=yes; path=/';</script>
    <form method="post" action="/submit">
    <input name="Email"><input name="Password" type="password"><button>Sign in</button></form>"""
    mfa_page = """<form method="post" action="/verify">
    <input name="Code"><button>Verify</button></form>"""

    async def route_request(route):
        request = route.request
        path = urlsplit(request.url).path
        seen.append(path)
        if path == "/submit":
            assert "js_executed=yes" in request.headers.get("cookie", "")
            fields = parse_qs(request.post_data)
            assert fields["Password"] == ["synthetic-password"]
            await route.fulfill(status=200, content_type="text/html", body=mfa_page)
        elif path == "/verify":
            fields = parse_qs(request.post_data)
            assert fields["Code"] == ["123456"]
            await route.fulfill(status=302, headers={"Location": callback})
        else:
            assert path == "/connect/authorize"
            await route.fulfill(status=200, content_type="text/html", body=password_page)

    await page.route("**/*", route_request)
    task = asyncio.create_task(login._watch(page))
    try:
        await page.locator('input[name="Email"]').fill("synthetic@example.invalid")
        await page.locator('input[name="Password"]').fill("synthetic-password")
        await page.get_by_role("button", name="Sign in").click()
        await page.locator('input[name="Code"]').fill("123456")
        await page.get_by_role("button", name="Verify").click(no_wait_after=True)
        assert await asyncio.wait_for(task, 5) == callback
        assert seen == ["/connect/authorize", "/submit", "/verify"]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_browser_429_reports_server_delay_without_retry(page):
    login = BrowserLogin()
    requests = []

    async def route_request(route):
        requests.append(route.request.url)
        await route.fulfill(status=429, headers={"Retry-After": "120"}, body="private body")

    await page.route("**/*", route_request)
    with pytest.raises(RateLimited) as error:
        await login._watch(page)
    assert error.value.retry_after == 120 and error.value.server_retry_after
    assert "private" not in str(error.value)
    assert len(requests) == 1


async def test_browser_rejects_wrong_state_from_real_redirect(page):
    await page.route(
        "**/*",
        lambda route: route.fulfill(
            status=302,
            headers={"Location": "com.myqops://android?code=test&state=wrong"},
        ),
    )
    with pytest.raises(AuthenticationError):
        await BrowserLogin()._watch(page)


async def test_browser_receives_scripted_callback(page):
    # Run a public HA-owned transaction in real Chrome; verifier stays with HA.
    owner = OAuthSession()
    login = BrowserLogin(oauth=BrowserAuthorization(create_request(owner)))
    callback = f"com.myqops://android?code=synthetic&state={login.oauth.state}"
    await page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=f"""<button onclick="location.href='{callback}'">Complete</button>""",
        ),
    )
    task = asyncio.create_task(login._watch(page))
    try:
        await page.get_by_role("button", name="Complete").click(no_wait_after=True)
        assert await asyncio.wait_for(task, 5) == callback
        assert owner.callback_code(callback) == "synthetic"
        assert page.url.startswith(IDENTITY)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("status", [403, 429])
async def test_browser_keeps_verification_page_open_until_user_completes(page, status):
    login = BrowserLogin()
    seen = []

    async def route_request(route):
        path = urlsplit(route.request.url).path
        seen.append(path)
        if path == "/connect/authorize":
            await route.fulfill(
                status=status,
                content_type="text/html",
                headers={
                    "cf-mitigated": "challenge",
                },
                body="""<button onclick="location.href='/ready'">Verify browser</button>""",
            )
        else:
            assert path == "/ready"
            await route.fulfill(status=200, content_type="text/html", body="Synthetic sign-in page")

    await page.route("**/*", route_request)
    task = asyncio.create_task(login._watch(page, connection_check=True))
    try:
        await page.get_by_role("button", name="Verify browser").wait_for()
        assert not task.done() and not page.is_closed()
        await page.get_by_role("button", name="Verify browser").click()
        assert await asyncio.wait_for(task, 5) is None
        assert login.browser_verification_seen
        assert seen == ["/connect/authorize", "/ready"]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

#!/usr/bin/env python3
"""Interactive read-only account probe. Never sends a door or settings command.

The browser handles password and MFA entry; HTTP/token diagnostics use hidden
terminal prompts. Reports contain allowlisted structure only. No tokens are saved.
"""

import argparse
import asyncio
import getpass
import importlib.util
import json
import ssl
import sys
import time
from contextlib import AsyncExitStack
from importlib.metadata import version
from pathlib import Path

import aiohttp
import certifi

# Load only the vendored API package; no Home Assistant installation is required.
_api = Path(__file__).resolve().parents[1] / "custom_components/myq/api"
_spec = importlib.util.spec_from_file_location("myq_protocol", _api / "__init__.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules["myq_protocol"] = _module
_spec.loader.exec_module(_module)

from myq_protocol.app_check import FileAppCheckProvider, SuppliedAppCheckProvider  # noqa: E402
from myq_protocol.auth import Challenge, Login  # noqa: E402
from myq_protocol.browser import BrowserLogin  # noqa: E402
from myq_protocol.client import Client  # noqa: E402
from myq_protocol.const import NATIVE_HEADERS  # noqa: E402
from myq_protocol.errors import (  # noqa: E402
    AppCheckError,
    AppCheckUnavailable,
    BrowserVerificationRequired,
    MyQError,
    RateLimited,
)
from myq_protocol.har import read_callback  # noqa: E402
from myq_protocol.oauth import OAuthSession  # noqa: E402
from myq_protocol.tokens import Tokens  # noqa: E402
from myq_protocol.video import Video  # noqa: E402


async def main(
    connection_check: bool = False,
    mode: str | None = None,
    use_curl: bool = False,
    curl_profile: str = "chrome_android",
    app_check: bool = False,
    app_check_file: Path | None = None,
) -> None:
    started = time.monotonic()

    def progress(message: str) -> None:
        print(f"[{time.monotonic() - started:5.1f}s] {message}", flush=True)

    print("Read-only MyQ probe. Browser sign-in handles password and MFA on MyQ's own pages.")
    print("Sign-in allows 10 minutes. API requests time out after 30 seconds. Ctrl-C cancels.")
    if use_curl and mode is None:
        mode = "p"
    # Deliberately read terminal input on the main thread so Ctrl-C also works
    # while getpass/input waits. There is no concurrent work during these prompts.
    if mode is None:
        mode = (
            "b"
            if connection_check
            else input(  # noqa: ASYNC250
                "Sign in with [b]rowser, experimental HTTP [p]assword, or [r]efresh token? [b]: "
            )
            .strip()
            .lower()
            or "b"
        )
    if mode not in {"b", "p", "r", "h"}:
        raise SystemExit("Choose b, p, or r; use --browser-har for capture-assisted sign-in.")
    login = None
    browser_login = None
    provider = FileAppCheckProvider(app_check_file) if app_check_file else None
    stage = "authorization_page"
    async with AsyncExitStack() as stack:
        transport_report = {
            "auth_transport": {
                "b": "browser",
                "h": "browser_har",
                "p": "curl_cffi" if use_curl else "experimental_http",
                "r": "refresh_token",
            }[mode],
            "http_transport": "curl_cffi" if use_curl else "aiohttp",
            "native_request_profile": dict(NATIVE_HEADERS),
        }
        if use_curl:
            try:
                from myq_protocol.curl_http import CurlHttp
            except ImportError:
                raise SystemExit(
                    "Install the optional transport: .venv/bin/python -m pip install '.[curl]'"
                ) from None
            http = await stack.enter_async_context(CurlHttp(curl_profile))
            client = Client(transport=http)
            transport_report.update(
                {"curl_profile": http.profile, "curl_cffi_version": version("curl_cffi")}
            )
            progress(
                f"Using curl_cffi {version('curl_cffi')}: {http.profile} for browser pages, "
                "a separate native profile for OAuth/API requests"
            )
        else:
            # The python.org macOS build may have no default CA file. Use an
            # explicit bundle instead of shell-specific SSL_CERT_FILE defaults.
            connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
            session = await stack.enter_async_context(aiohttp.ClientSession(connector=connector))
            client = Client(session)
        try:
            if mode in {"b", "h"}:
                if not connection_check:
                    stage = "app_check_source"
                    if provider is not None:
                        await provider.acquire()
                    elif not app_check:
                        raise AppCheckUnavailable()
                if mode == "h":
                    stage = "authorization_capture"
                    oauth = OAuthSession()
                    print(
                        "\nUse the normal Chrome profile that completed your MyQ login.\n"
                        "Open Developer Tools > Network, enable Preserve log, and clear the log.\n"
                        "Navigate to this fresh authorization URL and complete sign-in/MFA:\n"
                    )
                    print(oauth.authorize_url, flush=True)
                    print(
                        "\nAfter the app redirect, export the network log as HAR.\n"
                        "An app-opening error is OK. The default sanitized export is sufficient.\n"
                        "Keep this probe running: the URL belongs to its 10-minute PKCE session."
                    )
                    capture_path = input("Local path to the newly exported HAR: ").strip()  # noqa: ASYNC250
                    path = Path(capture_path.strip("\"'"))
                    callback_url = await asyncio.to_thread(read_callback, path, oauth)
                    progress("Validated the captured MyQ OAuth callback")
                else:
                    browser_login = BrowserLogin(progress)
                    oauth = browser_login.oauth
                    callback_url = await browser_login.authorize(connection_check=connection_check)
                    if connection_check:
                        print("\n--- BEGIN SANITIZED REPORT ---")
                        print(
                            json.dumps(
                                {
                                    "connection_check": "ok",
                                    **transport_report,
                                    "browser_page_loaded": True,
                                    "http_status": browser_login.last_status,
                                },
                                indent=2,
                            )
                        )
                        print("--- END SANITIZED REPORT ---")
                        return
                stage = "app_check_acquisition"
                if app_check:
                    print("Enter a current App Check token issued for your MyQ Android app.")
                    provider = SuppliedAppCheckProvider(
                        getpass.getpass("MyQ Firebase App Check token: ").strip()
                    )
                stage = "token_exchange"
                progress("Exchanging the browser authorization code for a session")
                try:
                    await oauth.complete(
                        callback_url,
                        client.auth,
                        app_check_provider=provider,
                        require_app_check=True,
                    )
                finally:
                    del callback_url
            elif connection_check:
                login = Login(client.http, client.auth, progress, unified=use_curl)
                # Stop at the public sign-in form. No username/password is sent.
                forms = await login._navigate("GET", login.authorize_url)
                print("\n--- BEGIN SANITIZED REPORT ---")
                print(
                    json.dumps(
                        {
                            "connection_check": "ok",
                            **transport_report,
                            "password_form_found": isinstance(forms, list)
                            and any({"Email", "Password"} <= form.inputs.keys() for form in forms),
                        },
                        indent=2,
                    )
                )
                print("--- END SANITIZED REPORT ---")
                return
            elif mode == "r":
                stage = "token_refresh"
                client.auth.tokens = Tokens("", getpass.getpass("Refresh token: ").strip(), 0)
                progress("Renewing the existing session")
                await client.auth.access_token()
            elif mode == "p":
                username = input("MyQ email: ").strip()  # noqa: ASYNC250
                password = getpass.getpass("MyQ password: ")
                login = Login(client.http, client.auth, progress, unified=use_curl)
                try:
                    result = await login.start(username, password)
                finally:
                    del password
                while isinstance(result, Challenge):
                    if result.kind == "method":
                        choices = list(result.choices)
                        # Labels may include masked contact information; show them
                        # only locally, outside the sanitized report below.
                        for index, key in enumerate(choices, start=1):
                            print(f"  {index}. {result.choices[key]}")
                        selection = input("Verification method number: ").strip()  # noqa: ASYNC250
                        if not selection.isdigit() or not 1 <= int(selection) <= len(choices):
                            print("Choose one of the displayed numbers.")
                            continue
                        value = choices[int(selection) - 1]
                    else:
                        progress("Waiting for the verification code sent by MyQ")
                        value = getpass.getpass("Verification code: ")
                    previous_kind = result.kind
                    try:
                        result = await login.respond(value)
                    finally:
                        del value
                    if isinstance(result, Challenge) and previous_kind == result.kind == "code":
                        print("MyQ requested a code again; verification has not completed.")
            stage = "profile"
            progress("Reading the authenticated profile")
            profile = await client.profile()
            stage = "device_discovery"
            progress("Reading registered accounts and devices")
            devices = await client.discover()
            report = []
            for device in devices.values():
                row = {
                    "type": device.device_type,
                    "family": device.family,
                    "online": device.online,
                    "state_fields": sorted(device.state),
                    "can_open": device.can_move("open"),
                    "can_close": device.can_move("close"),
                }
                if device.is_camera:
                    try:
                        stage = "camera_capabilities"
                        progress("Reading camera capabilities")
                        details = await Video(client).details(device)
                        row["camera_settings_fields"] = {
                            k: sorted(v) if isinstance(v, dict) else type(v).__name__
                            for k, v in details["settings"].items()
                        }
                        row["camera_capability_fields"] = sorted(details["cap"])
                    except MyQError as error:
                        row["camera_error"] = type(error).__name__
                report.append(row)
            print("\n--- BEGIN SANITIZED REPORT ---")
            print(
                json.dumps(
                    {
                        "authenticated_profile": bool(profile.get("user_id")),
                        **transport_report,
                        "device_count": len(report),
                        "devices": report,
                    },
                    indent=2,
                )
            )
            print("--- END SANITIZED REPORT ---")
        except MyQError as error:
            extra = {"http_status": client.http.last_status} if use_curl else {}
            if isinstance(error, AppCheckError):
                extra["app_check_status"] = error.reason
            if client.auth.last_exchange is not None:
                extra["token_exchange"] = client.auth.last_exchange
            if isinstance(error, RateLimited):
                extra.update(
                    {
                        "retry_after_seconds": round(error.retry_after),
                        "retry_after_source": "server"
                        if error.server_retry_after
                        else "local default",
                    }
                )
            print("\n--- BEGIN SANITIZED REPORT ---", file=sys.stderr)
            print(
                json.dumps(
                    {
                        "error": type(error).__name__,
                        "message": str(error),
                        "stage": stage,
                        "browser_verification_seen": bool(
                            isinstance(error, BrowserVerificationRequired)
                            or (browser_login and browser_login.browser_verification_seen)
                            or (use_curl and client.http.browser_verification_seen)
                        ),
                        **transport_report,
                        "last_sign_in_forms": login.form_structure if login else [],
                        **extra,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            print("--- END SANITIZED REPORT ---", file=sys.stderr)
            raise SystemExit(1) from None
        finally:
            if provider is not None:
                provider.invalidate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--connection-check",
        action="store_true",
        help="Load only the public sign-in form without asking for credentials",
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--browser",
        dest="mode",
        action="store_const",
        const="b",
        help="Use interactive Chrome, matching the app's unified browser flow",
    )
    transport.add_argument(
        "--browser-har",
        dest="mode",
        action="store_const",
        const="h",
        help="Sign in using a fresh HAR from your normal browser and this probe's PKCE session",
    )
    transport.add_argument(
        "--http",
        dest="mode",
        action="store_const",
        const="p",
        help="Use the experimental HTTP form transport for diagnostics",
    )
    transport.add_argument(
        "--refresh",
        dest="mode",
        action="store_const",
        const="r",
        help="Renew an existing token entered at a hidden terminal prompt",
    )
    parser.add_argument(
        "--curl",
        action="store_true",
        help="Use optional curl_cffi for unified HTTP sign-in and API requests",
    )
    proof_source = parser.add_mutually_exclusive_group()
    proof_source.add_argument(
        "--app-check",
        action="store_true",
        help="Prompt secretly for a genuine MyQ App Check token; requires browser or browser-har",
    )
    proof_source.add_argument(
        "--app-check-file",
        type=Path,
        help="Read current proof from a private JSON or raw JWT file just before exchange",
    )
    parser.add_argument(
        "--curl-profile",
        choices=("chrome_android", "chrome"),
        help="curl_cffi browser profile (default: chrome_android); requires --curl",
    )
    args = parser.parse_args()
    if args.curl_profile and not args.curl:
        parser.error("--curl-profile requires --curl")
    if args.connection_check and args.mode in {"r", "h"}:
        parser.error("--connection-check cannot be combined with --refresh or --browser-har")
    if (args.app_check or args.app_check_file) and (
        args.mode not in {"b", "h"} or args.connection_check
    ):
        parser.error("App Check options require --browser or --browser-har sign-in")
    try:
        asyncio.run(
            main(
                args.connection_check,
                args.mode,
                args.curl,
                args.curl_profile or "chrome_android",
                args.app_check,
                args.app_check_file,
            )
        )
    except (KeyboardInterrupt, EOFError):
        print("\nProbe cancelled. No door or camera commands were sent.", file=sys.stderr)
        raise SystemExit(130) from None

#!/usr/bin/env python3
"""Local native-app auth observer; never initiates or replays MyQ requests.

Run in the isolated capture environment. Only the two specified HTTPS hosts
are decrypted. Passwords, codes, cookies, OAuth tokens, and raw flows are not
saved. App Check tokens are saved separately with owner-only permissions.
"""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import ssl
import tempfile
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

IDENTITY = "partner-identity.myq-cloud.com"
FIREBASE = "firebaseappcheck.googleapis.com"
ALLOW_HOSTS = [rf"^({re.escape(IDENTITY)}|{re.escape(FIREBASE)}):443$"]
PUBLIC_FIELDS = {"client_id", "redirect_uri", "scope", "grant_type", "code_challenge_method"}
PRESENCE_FIELDS = {"code", "code_verifier", "refresh_token", "client_secret"}
MAX_BODY = 128 * 1024
TLS_CHECK_PATH = "/__myq_capture_tls_check"


def endpoint(host: str, path: str) -> str | None:
    """Return a fixed label, never a URL containing an account ID or query."""
    path = urlsplit(path).path
    if host == IDENTITY and path in {"/connect/authorize", "/connect/token"}:
        return path.rsplit("/", 1)[-1]
    if host == FIREBASE:
        match = re.fullmatch(
            r"/v1(?:beta)?/projects/[^/]+/apps/[^/]+:"
            r"(exchangeAppAttestAttestation|exchangeAppAttestAssertion|"
            r"exchangeDeviceCheckToken|exchangePlayIntegrityToken|generateAppAttestChallenge)",
            path,
        )
        if match:
            return match[1]
    return None


def public_form(encoded: str) -> dict:
    fields = parse_qs(encoded, keep_blank_values=True, max_num_fields=64)
    result = {}
    for key in PUBLIC_FIELDS:
        values = fields.get(key, [])
        if len(values) == 1 and len(values[0]) <= 300:
            # Redirect parameters can contain secrets; preserve only its base URI.
            value = values[0]
            if key == "redirect_uri":
                uri = urlsplit(value)
                value = f"{uri.scheme}://{uri.hostname or ''}{uri.path}"
            result[key] = value
    for key in PRESENCE_FIELDS:
        result[f"{key}_present"] = bool(fields.get(key))
    return result


def write_private(path: Path, content: bytes) -> None:
    """Atomic owner-only output; never follows an existing output symlink."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class AuthCapture:
    def __init__(
        self, session: Path, onboarding_host: str = "mitm.it", tls_check_port: int | None = None
    ):
        self.session = session
        self.onboarding_host = onboarding_host
        self.tls_check_port = tls_check_port
        self.events = []
        self.token_hashes = set()
        self.errors = 0
        self.connections = 0
        self.certificate_page_requests = 0
        self.auth_host_requests = 0
        self.myq_connect_hosts = set()
        self.tls_check_requests = 0
        self.tls_checks_from_other_devices = 0
        self.direct_tls_check_requests = 0
        self.http_tls_check_redirects = 0
        self.run_info = {}
        self.tls_failures = []

    def running(self) -> None:
        self.run_info["status"] = "running"
        self.flush()

    def client_connected(self, client) -> None:
        self.connections += 1
        self.flush()

    def http_connect(self, flow) -> None:
        host = flow.request.host.lower()
        if len(self.myq_connect_hosts) < 32 and (
            host in {IDENTITY, FIREBASE} or host.endswith((".myq-cloud.com", ".myq.com"))
        ):
            self.myq_connect_hosts.add(host)
            self.flush()

    def request(self, flow) -> None:
        if (
            self.tls_check_port is not None
            and flow.request.host == self.onboarding_host
            and getattr(flow.request, "port", None) == self.tls_check_port
            and getattr(flow.request, "scheme", "") == "http"
            and getattr(flow.request, "method", "") == "GET"
        ):
            from mitmproxy import http

            self.http_tls_check_redirects += 1
            flow.response = http.Response.make(
                303,
                b"Open the HTTPS certificate trust check.\n",
                {
                    "Location": f"https://{self.onboarding_host}:{self.tls_check_port}/",
                    "Cache-Control": "no-store",
                },
            )
            self.flush()
            return
        if (
            flow.request.host == FIREBASE
            and getattr(flow.request, "scheme", "") == "https"
            and getattr(flow.request, "method", "") == "GET"
            and urlsplit(flow.request.path).path == TLS_CHECK_PATH
        ):
            from mitmproxy import http

            self.tls_check_requests += 1
            peer = flow.client_conn.peername
            remote = bool(peer and peer[0] not in {self.onboarding_host, "127.0.0.1", "::1"})
            self.tls_checks_from_other_devices += int(remote)
            origin = "another device" if remote else "this Mac"
            flow.response = http.Response.make(
                200,
                f"MYQ CAPTURE HTTPS CHECK: OK\nRequest received from {origin}.\n"
                "This diagnostic page was generated locally by your Mac's capture proxy.\n"
                "No Firebase or MyQ login request was sent.\n",
                {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
            )
        if flow.request.host == self.onboarding_host:
            self.certificate_page_requests += 1
        elif flow.request.host in {IDENTITY, FIREBASE}:
            self.auth_host_requests += 1
        else:
            return
        self.flush()

    def save_token(self, token: str) -> bool:
        # A captured value is not a verified JWT or proof of cross-platform use.
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{20,16384}", token):
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        if digest not in self.token_hashes:
            write_private(
                self.session / f"app-check-{len(self.token_hashes) + 1}.txt", token.encode()
            )
            self.token_hashes.add(digest)
        return True

    def record(self, flow) -> None:
        request = flow.request
        label = endpoint(request.host, request.path)
        if not label or len(self.events) >= 200:
            return
        event = {"endpoint": label, "http_status": flow.response.status_code}
        if label == "authorize":
            event.update(public_form(urlsplit(request.path).query))
        elif label == "token":
            body = request.raw_content or b""
            if len(body) <= MAX_BODY:
                event.update(public_form(body.decode("utf-8", errors="strict")))
            event["app_check_present"] = False
            for header in ("Firebase-AppCheck-Token", "X-Firebase-AppCheck"):
                token = request.headers.get(header, "")
                if token:
                    event["app_check_present"] = True
                    event["app_check_saved"] = self.save_token(token)
        response_body = flow.response.raw_content or b""
        if label != "authorize" and len(response_body) <= MAX_BODY:
            try:
                data = json.loads(flow.response.get_text(strict=True) or "null")
            except (ValueError, UnicodeError):
                data = None
            if isinstance(data, dict):
                if label.startswith("exchange") and flow.response.status_code == 200:
                    event["app_check_saved"] = self.save_token(data.get("token", ""))
                if label == "token":
                    for key in ("access_token", "refresh_token", "id_token"):
                        event[f"{key}_returned"] = isinstance(data.get(key), str)
                    if data.get("error") in {"invalid_grant", "invalid_client", "invalid_request"}:
                        event["oauth_error"] = data["error"]
                    if data.get("code") in {"401.102", "401.122", "401.123", "401.125"}:
                        event["myq_code"] = data["code"]
        self.events.append(event)
        self.flush()
        print(json.dumps(event), flush=True)

    def flush(self) -> None:
        report = {
            "events": self.events,
            "app_check_tokens_saved": len(self.token_hashes),
            "capture_errors": self.errors,
            "run": self.run_info,
            "tls_failures": self.tls_failures,
            "connection_diagnostics": {
                "client_connections": self.connections,
                "certificate_page_requests": self.certificate_page_requests,
                "decrypted_auth_host_requests": self.auth_host_requests,
                "myq_connect_hosts": sorted(self.myq_connect_hosts),
                "tls_check_requests": self.tls_check_requests,
                "tls_checks_from_other_devices": self.tls_checks_from_other_devices,
                "direct_tls_check_requests": self.direct_tls_check_requests,
                "http_tls_check_redirects": self.http_tls_check_redirects,
            },
        }
        write_private(self.session / "sanitized-report.json", json.dumps(report, indent=2).encode())

    def response(self, flow) -> None:
        try:
            self.record(flow)
        except Exception:
            # Framework exceptions can contain request URLs or credential values.
            self.errors += 1
            print("Capture observation failed; raw error withheld.", flush=True)

    def tls_failed_client(self, data) -> None:
        self.record_tls_failure(data, "client")

    def tls_failed_server(self, data) -> None:
        self.record_tls_failure(data, "upstream")

    def record_tls_failure(self, data, side: str) -> None:
        address = data.context.server.address
        candidates = [address[0] if address else "", data.context.client.sni or ""]
        host = next((h for h in candidates if h in {IDENTITY, FIREBASE}), None)
        if host is None or len(self.tls_failures) >= 50:
            return
        raw_error = (data.conn.error or "").lower()
        reason = "unclassified"
        for fragment, label in [
            ("unknown ca", "unknown_ca"),
            ("certificate unknown", "certificate_unknown"),
            ("bad certificate", "bad_certificate"),
            ("certificate verify failed", "certificate_verification_failed"),
            ("closed", "connection_closed"),
        ]:
            if fragment in raw_error:
                reason = label
                break
        peer = data.context.client.peername
        event = {
            "side": side,
            "host_role": "myq_identity" if host == IDENTITY else "firebase_app_check",
            "reason": reason,
            "from_other_device": bool(
                peer and peer[0] not in {self.onboarding_host, "127.0.0.1", "::1"}
            ),
        }
        self.tls_failures.append(event)
        self.flush()
        print(json.dumps({"tls_failure": event}), flush=True)


def prepare() -> Path:
    from mitmproxy.certs import CertStore

    session = Path(tempfile.mkdtemp(prefix="myq-native-capture-"))
    conf = session / "certificates"
    conf.mkdir(mode=0o700)
    CertStore.from_store(conf, "mitmproxy", 2048)
    write_private(session / "capture-session", b"myq-native-capture-v1\n")
    print(f"Prepared private capture directory: {session}")
    print("No listener started and no phone or Mac trust settings changed.")
    return session


def validate_session(session: Path) -> None:
    if (
        session.is_symlink()
        or session.stat().st_uid != os.getuid()
        or session.stat().st_mode & 0o077
        or (session / "capture-session").read_bytes() != b"myq-native-capture-v1\n"
    ):
        raise SystemExit("Use the private directory created by --prepare.")


def direct_tls_context(session: Path, host: str) -> ssl.SSLContext:
    """Issue a local IP certificate from the existing capture CA, not a new CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from mitmproxy.certs import CertStore

    directory = session / "certificates"
    store = CertStore.from_store(directory, "mitmproxy", 2048)
    leaf = store.get_cert(host, [x509.IPAddress(ipaddress.ip_address(host))])
    cert_file = directory / "local-check-cert.pem"
    key_file = directory / "local-check-key.pem"
    write_private(cert_file, leaf.cert.to_pem() + store.default_ca.to_pem())
    write_private(
        key_file,
        leaf.privatekey.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


async def serve_direct_tls_check(reader, writer, *, capture: AuthCapture) -> None:
    """Serve one fixed diagnostic; discard request headers without logging."""
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        capture.direct_tls_check_requests += 1
        capture.flush()
        body = (
            b"MYQ CERTIFICATE TRUST CHECK: OK\n"
            b"This page is served directly by your Mac using the capture CA.\n"
            b"No MyQ login or Firebase request was sent.\n"
        )
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
    except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def run(
    session: Path,
    host: str,
    port: int,
    minutes: int,
    passthrough: bool = False,
    firebase_only: bool = False,
    tls_check_port: int | None = None,
) -> None:
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    address = ipaddress.ip_address(host)
    if address.version != 4 or not address.is_private or address.is_unspecified:
        raise SystemExit("Choose the Mac's local IPv4 address, or 127.0.0.1 for testing.")
    await asyncio.to_thread(validate_session, session)
    capture_hosts = [rf"^{re.escape(FIREBASE)}:443$"] if firebase_only else ALLOW_HOSTS
    options = Options(
        listen_host=host,
        listen_port=port,
        confdir=str(session / "certificates"),
        # Match no HTTPS host in recovery mode; CONNECT tunnels stay encrypted.
        allow_hosts=[r"(?!)"] if passthrough else capture_hosts,
        ssl_insecure=False,
    )
    master = DumpMaster(options, with_termlog=False, with_dumper=False)
    # Serve the setup page directly on our listening address. Safari may upgrade
    # mitm.it to HTTPS, which is intentionally outside the decryption allowlist
    # and then displays the public site's misleading "not going through" page.
    master.options.update(stream_large_bodies="128k", onboarding_host=host)
    # The built-in installer otherwise matches every port on its configured host,
    # concealing an accidental HTTP request to our separate HTTPS diagnostic.
    master.addons.get("onboarding").port = port
    capture = AuthCapture(session, onboarding_host=host, tls_check_port=tls_check_port)
    started = datetime.now(UTC)
    capture.run_info = {
        "status": "starting",
        "mode": "passthrough" if passthrough else "firebase_only" if firebase_only else "auth",
        "started_at_utc": started.isoformat(),
        "scheduled_stop_at_utc": (started + timedelta(minutes=minutes)).isoformat(),
    }
    master.addons.add(capture)
    loop = asyncio.get_running_loop()
    timer = loop.call_later(minutes * 60, master.shutdown)
    print(f"Starting proxy on {host}:{port}; stops after {minutes} minutes or Ctrl-C.")
    if passthrough:
        print("Recovery mode: all HTTPS passes through encrypted; auth capture is disabled.")
    elif firebase_only:
        print("Firebase-only mode: MyQ identity HTTPS passes through without decryption.")
    print(f"Private capture directory: {session}")
    print(f"Open this exact address on the phone: http://{host}:{port}/")
    print("Phone setup and cleanup: docs/research/native-capture.md", flush=True)
    diagnostic_server = None
    try:
        if tls_check_port:
            context = await asyncio.to_thread(direct_tls_context, session, host)
            diagnostic_server = await asyncio.start_server(
                partial(serve_direct_tls_check, capture=capture),
                host,
                tls_check_port,
                ssl=context,
                limit=8192,
            )
            print(f"Direct certificate trust check: https://{host}:{tls_check_port}/", flush=True)
        await master.run()
    finally:
        timer.cancel()
        if diagnostic_server:
            diagnostic_server.close()
            await diagnostic_server.wait_closed()
        capture.run_info.update(status="stopped", stopped_at_utc=datetime.now(UTC).isoformat())
        capture.flush()
        print("Stopped. Turn off the iPhone proxy and remove the capture certificate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--session", type=Path)
    parser.add_argument("--listen-host")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--minutes", type=int, choices=range(1, 31), default=15)
    parser.add_argument(
        "--tls-check-port", type=int, help="Optional direct local HTTPS trust check"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--passthrough", action="store_true", help="Recover connectivity without HTTPS inspection"
    )
    mode.add_argument(
        "--firebase-only",
        action="store_true",
        help="Inspect only Firebase App Check; tunnel MyQ identity without decryption",
    )
    args = parser.parse_args()
    # Suppress mitmproxy's raw flow/URL/exception logs; our output is allowlisted.
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        if args.prepare:
            prepare()
        elif args.session and args.listen_host and 1 <= args.port <= 65535:
            if args.tls_check_port is not None and (
                not 1 <= args.tls_check_port <= 65535 or args.tls_check_port == args.port
            ):
                parser.error("Choose a distinct valid port for --tls-check-port.")
            asyncio.run(
                run(
                    args.session,
                    args.listen_host,
                    args.port,
                    args.minutes,
                    args.passthrough,
                    args.firebase_only,
                    args.tls_check_port,
                )
            )
        else:
            parser.error("Use --prepare, or --session PATH --listen-host MAC_LOCAL_IP.")
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit(
            "Capture failed; raw error withheld. Check paths, dependencies and port."
        ) from None

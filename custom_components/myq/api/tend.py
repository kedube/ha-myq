"""Tend's TLS/Cxnet signaling, recovered from Android 5.324 (not media bytes)."""

import asyncio
import json
import re
import ssl
import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    AccessDenied,
    CameraWakeFailed,
    CannotConnect,
    CommandRejected,
    MyQError,
    ProtocolError,
)

HOST = "server.seedonk.com"
PORT = 5104
HANDSHAKE_TIMEOUT = 5
# A video request that asks Tend to wake the camera is answered once the camera is up:
# allow it the 30 seconds the app's HTTP wake-up waits (DevicesManager's fallback).
WAKEUP_ANSWER_TIMEOUT = 30
LOGIN_TIMEOUT = 15
MAX_LOGIN_FRAMES = 5
KEEPALIVE_INTERVAL = 35
MAX_NAKS = 10  # ConnectionManager.run: "more than 10 consecutive NAKs"
MAX_FRAME_DATA = 1_024_000
SERVICE = 121  # The header's service field; Tend's video and audio channels use 122.
# deny-video-receive / deny-audio-receive reasons when Tend's video service was asked to
# wake the camera (MediaConnectionManager.denyWakeReasonToLiveStreamErrorCode).
WAKE_DENIALS = {
    "wake-timeout": "The camera did not wake up in time",
    "wake-unavailable": "The camera cannot be woken right now",
    "wake-busy": "The camera is already being woken; try again shortly",
}
_HEADER = struct.Struct("<BBHHBIHHH2sQ")
_tls_context: ssl.SSLContext | None = None


def _build_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    try:
        # Home Assistant ships certifi; a host without a system CA store still verifies.
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return context


async def _client_tls_context() -> ssl.SSLContext:
    """Load the CA store once, off the event loop, and reuse it for every session."""
    global _tls_context
    if _tls_context is None:
        # Concurrent first callers may each build one; any of them is equivalent.
        _tls_context = await asyncio.to_thread(_build_tls_context)
    return _tls_context


@dataclass(repr=False)
class Frame:
    """One Cxnet frame; payloads and session identifiers must not enter logs."""

    action: int
    sender: bytes = field(default=b"\0" * 16, repr=False)
    recipient: int = 0
    options: bytes = field(default=b"", repr=False)
    data: bytes = field(default=b"", repr=False)
    service: int = SERVICE

    def encode(self) -> bytes:
        header_length = _HEADER.size + len(self.sender) + len(self.options)
        if len(self.sender) != 16 or header_length > 32767 or len(self.data) > MAX_FRAME_DATA:
            raise ProtocolError("Invalid Tend signaling frame")
        return (
            _HEADER.pack(
                65,
                3,
                header_length,
                len(self.sender),
                0,
                len(self.data),
                self.service,
                self.action,
                2,
                b"**",
                self.recipient,
            )
            + self.sender
            + self.options
            + self.data
        )

    @classmethod
    async def read(cls, reader: asyncio.StreamReader) -> "Frame":
        header = await reader.readexactly(_HEADER.size)
        name, version, size, sender_size, _, data_size, _, action, crypto, _, recipient = (
            _HEADER.unpack(header)
        )
        if (
            name != 65
            or version not in (1, 2, 3)
            or crypto not in (0, 2)
            or sender_size != 16
            or not _HEADER.size + sender_size <= size <= 32767
            or data_size > MAX_FRAME_DATA
        ):
            raise ProtocolError("Invalid Tend signaling frame")
        body = await reader.readexactly(size - _HEADER.size + data_size)
        options_end = size - _HEADER.size
        return cls(
            action, body[:sender_size], recipient, body[sender_size:options_end], body[options_end:]
        )


def properties(values: dict[str, str]) -> bytes:
    """Write the Java Properties subset used by Cxnet authentication and routing."""

    def escape(value: str) -> str:
        result = []
        for char in value:
            if char in "\\ :=#!":
                result.append("\\" + char)
            elif char in "\n\r\t\f":
                result.append({"\n": "\\n", "\r": "\\r", "\t": "\\t", "\f": "\\f"}[char])
            elif ord(char) > 127:
                for offset in range(0, len(encoded := char.encode("utf-16-be")), 2):
                    result.append("\\u" + encoded[offset : offset + 2].hex())
            else:
                result.append(char)
        return "".join(result)

    return "".join(f"{escape(key)}={escape(value)}\n" for key, value in values.items()).encode(
        "ascii"
    )


def parse_properties(payload: bytes) -> dict[str, str]:
    """Read escaped Java Properties without treating server contents as code."""

    def unescape(value: str) -> str:
        def replace(match: re.Match) -> str:
            escaped = match.group(1)
            if escaped.startswith("u"):
                try:
                    return chr(int(escaped[1:], 16))
                except ValueError:
                    raise ProtocolError("Invalid Tend signaling properties") from None
            return {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(escaped, escaped)

        value = re.sub(r"\\(u.{4}|.)", replace, value)
        try:
            return value.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeError:
            raise ProtocolError("Invalid Tend signaling properties") from None

    result = {}
    pending = ""
    # Java ends a line at \n, \r or \r\n only; str.splitlines() would also cut values
    # at form feeds, U+0085 and other separators.
    for line in re.split(r"\r\n|\r|\n", payload.decode("iso-8859-1")):
        line = pending + line.lstrip(" \t\f")
        if (len(line) - len(line.rstrip("\\"))) % 2:
            pending = line[:-1]
            continue
        pending = ""
        if not line or line.startswith(("#", "!")):
            continue
        escaped = False
        index = len(line)
        for i, char in enumerate(line):
            if char in "=: \t\f" and not escaped:
                index = i
                break
            escaped = char == "\\" and not escaped
        rest = line[index:].lstrip(" \t\f")
        if rest.startswith(("=", ":")):
            rest = rest[1:].lstrip(" \t\f")
        result[unescape(line[:index])] = unescape(rest)
    if pending:
        raise ProtocolError("Incomplete Tend signaling properties")
    return result


def _alias(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(char) < 32 or char == "|" for char in value)
    ):
        raise ProtocolError("Missing or invalid Tend camera/user alias")
    return value


def trusted_host(host: str) -> bool:
    """A host name within Tend's own DNS domains."""
    return (
        len(host) <= 253
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is not None
        and all(
            label and not label.startswith("-") and not label.endswith("-")
            for label in host.split(".")
        )
        and any(
            host == domain or host.endswith("." + domain)
            for domain in ("seedonk.com", "tendplatform.com")
        )
    )


def denial(reason: str) -> MyQError:
    """The error for a deny-video-receive or deny-audio-receive reason."""
    if (message := WAKE_DENIALS.get(reason.strip().lower())) is not None:
        return CameraWakeFailed(message)
    return AccessDenied("The camera declined live video")


def _redirect(response: Frame, login: dict[str, str], current_port: int) -> tuple[str, int] | None:
    """Honor only observed hub/datacenter redirects within Tend's DNS domains."""
    # The front door answers "1=<address>:<port>" and "redirectHost=<broker name>";
    # an answer that carries a session is never a redirect.
    session = bool(login.keys() & {"alias", "sid", "session-id"})
    if response.action == 519 and "redirectHost" in login and not session:
        host, port = login["redirectHost"], current_port
    elif response.action == 519 and "1" in login and not session:
        parts = login["1"].split(":")
        # isdigit() alone also accepts digits int() cannot read, such as "²".
        port_text = parts[1] if len(parts) == 2 else ""
        if not (port_text.isascii() and port_text.isdigit()) or not 1 <= int(port_text) <= 65535:
            raise ProtocolError("Invalid Tend hub redirect")
        host, port = parts[0], current_port  # The hub's port is the plaintext port.
    elif response.action == 1 and response.options == struct.pack("<I", 533):
        if set(login) == {"host"} and "&" in login["host"]:
            login = parse_properties(response.data.replace(b"&", b"\n"))
        host = login.get("host", "")
        port_text = login.get("sslPort", "")
        if not (port_text.isascii() and port_text.isdigit()) or not 1 <= int(port_text) <= 65535:
            raise ProtocolError("Invalid Tend datacenter redirect")
        port = int(port_text)
    else:
        return None
    host = host.lower()
    if not trusted_host(host):
        raise ProtocolError("Tend redirect left the trusted camera service")
    return host, port


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _messages(response: Frame) -> list[str]:
    """Decode the SDK's direct messages and action-501 batched push records.

    Not every broker push is text. Bytes that are not UTF-8 are replaced, so such a
    push matches no command instead of ending the session.
    """
    if response.action == 438:
        recipient = str(response.recipient) if response.recipient else "0" * 32
        options = response.options.decode("utf-8", "replace")
        return [f"videomid|system(-1)|{recipient}|{options}|{_text(response.data)}|"]
    if response.action == 501:
        messages = []
        offset = 0
        while offset < len(response.data):
            if len(response.data) - offset < 5:
                raise ProtocolError("Invalid Tend push record")
            kind = response.data[offset]
            length = struct.unpack_from("<I", response.data, offset + 1)[0]
            offset += 5
            if kind not in (0, 1) or length > len(response.data) - offset:
                raise ProtocolError("Invalid Tend push record")
            if kind == 0:
                messages.append(_text(response.data[offset : offset + length]))
            offset += length
        return messages
    if response.action in (410, 413, 414, 503, 506, 2003):
        return []  # Binary and legacy media payloads are not command strings.
    return [_text(response.data)] if response.data else []


class TendSession:
    """One viewer's authenticated signaling session, with no command replay."""

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._sender = b"\0" * 16
        self._recipient = 0
        self._device_alias = ""
        self._user_alias = ""
        self._video_mode = 12
        self._video_alias = ""
        self._requested = False
        self._authenticated = False
        self._tasks: list[asyncio.Task] = []
        self._closed: asyncio.Future | None = None
        self._failure: MyQError | None = None
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._naks = 0
        # f81/b: one CorrelationID per view, reused by its stop-video-receive.
        self._correlation = str(uuid.uuid4())
        self.on_status: Callable[[], None] | None = None
        # The video request's wake field: 1 asks Tend's video service to wake the
        # camera, 0 says it need not, None leaves the field out.
        self.wakeup: int | None = None
        # The camera may be asleep, so it gets longer to answer the video request.
        self.camera_asleep = False

    def _require_video(self, alias: str) -> str:
        """The video request; the app appends "wakeup=1|" or "wakeup=0|" when Tend's
        video service, not the client, wakes the camera (enable_vmw_wakeup)."""
        field = "" if self.wakeup is None else f"wakeup={1 if self.wakeup else 0}|"
        return f"require-video-send|{alias}|{self._video_mode}|{field}"

    @property
    def _answer_timeout(self) -> float:
        return WAKEUP_ANSWER_TIMEOUT if self.camera_asleep else HANDSHAKE_TIMEOUT

    async def control(
        self, command: str, actions: tuple[tuple[str, str], ...], *, cleanup: bool = False
    ) -> None:
        """Send one typed native control; a socket write is not a device acknowledgement."""
        allowed = {
            "set-ptz": {
                ("P", "-1"),
                ("P", "1"),
                ("T", "-1"),
                ("T", "1"),
                ("Z", "0"),
                ("Z", "1"),
                ("S", "0"),
            },
            "trigger-siren-action": {("31", "01"), ("31", "02")},
        }
        if command not in allowed or len(actions) != 1 or actions[0] not in allowed[command]:
            raise CommandRejected("Unsupported native camera command")
        stopping = (command, actions) in {
            ("set-ptz", (("S", "0"),)),
            ("trigger-siren-action", (("31", "02"),)),
        }
        if cleanup and not stopping:
            raise CommandRejected("Only camera stop commands are allowed during cleanup")
        if (
            not self._authenticated
            or not self._requested
            or self._closing
            or self._writer is None
            or self._closed is None
            or (self._closed.done() and not cleanup)
        ):
            raise CommandRejected("Open a live view before controlling this camera")
        parts = [command, self._video_alias or self._user_alias, *actions[0], ""]
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                await self._command("|".join(parts))
        except (OSError, TimeoutError):
            self._fail(CannotConnect("Camera command outcome is unknown; it was not retried"))
            raise CannotConnect("Camera command outcome is unknown; it was not retried") from None

    @property
    def connected(self) -> bool:
        return (
            self._authenticated
            and not self._closing
            and self._writer is not None
            and self._closed is not None
            and not self._closed.done()
        )

    async def send_to(
        self, device_alias: str, command: str, actions: tuple[tuple[str, str], ...]
    ) -> None:
        """Send one native command over a logged-in session, as the app's always-on
        Tend session does (g81/f): siren on/off, or move to a saved PTZ preset."""
        allowed = command == "trigger-siren-action" and actions in (
            (("31", "01"),),
            (("31", "02"),),
        )
        allowed = allowed or (
            command == "set-ptz"
            and len(actions) == 1
            and actions[0][0] == "G"
            and re.fullmatch(r"[0-9]{1,6}", actions[0][1]) is not None
        )
        if not allowed:
            raise CommandRejected("Unsupported native camera command")
        if not self.connected:
            raise CommandRejected("The camera command connection is not open")
        data = "|".join([command, self._user_alias, *actions[0], ""]).encode("utf-8")
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                await self._send(
                    411,
                    options=properties(
                        {"CX_DSTID": _alias(device_alias), "CorrelationID": str(uuid.uuid4())}
                    ),
                    data=data,
                )
        except (OSError, TimeoutError):
            self._fail(CannotConnect("Camera command outcome is unknown; it was not retried"))
            raise CannotConnect("Camera command outcome is unknown; it was not retried") from None

    async def _connect(self, host: str, port: int) -> None:
        context = await _client_tls_context()
        # The front door redirects each login to a broker (bNNN.broker.tend-us...), and
        # every broker presents the front door's own certificate, which names only
        # server.seedonk.com and server.tend-us.tendplatform.com. Checking a broker
        # against its own host name therefore always fails; whichever host answers
        # must prove it holds the front door's certificate instead.
        self._reader, self._writer = await asyncio.open_connection(
            host,
            port,
            ssl=context,
            server_hostname=HOST,
            ssl_handshake_timeout=LOGIN_TIMEOUT,
        )

    async def _send(self, action: int, *, options: bytes = b"", data: bytes = b"") -> None:
        async with self._write_lock:
            if self._writer is None or self._closing:
                raise CannotConnect("Tend signaling is not connected")
            self._writer.write(Frame(action, self._sender, self._recipient, options, data).encode())
            await self._writer.drain()

    async def _command(self, command: str, *, correlation: str | None = None) -> None:
        await self._send(
            411,
            options=properties(
                {
                    "CX_DSTID": self._device_alias,
                    "CorrelationID": correlation or str(uuid.uuid4()),
                }
            ),
            data=command.encode("utf-8"),
        )

    def _client_properties(self, access_token: str) -> dict[str, str]:
        return {
            "CX_UNAME": "",
            "CX_PASSWD": "myQ.accessToken:" + access_token,
            "os_name": "Android",
            "os_version": "17",
            "partner-id": "myQ",
            "sdk_version": "6.0.11698",
            "current_version": "5.324.0.1132442",
            "app-id": "com.chamberlain.android.liftmaster.myq",
        }

    async def _login_reply(self) -> tuple[Frame, dict[str, str]]:
        """The login's answer; a broker may push unrelated frames ahead of it."""
        for _ in range(MAX_LOGIN_FRAMES):
            response = await self._read_frame()
            if response.action in (1, 519):
                return response, parse_properties(response.data)
        raise ProtocolError("Unexpected Tend login response")

    def _logged_in(self, login: dict[str, str]) -> None:
        self._user_alias = _alias(login.get("alias"))

    async def _login(self, access_token: str) -> None:
        """The native IM login precedes both status listening and live-view requests."""
        async with asyncio.timeout(LOGIN_TIMEOUT):
            host, port = HOST, PORT
            for attempt in range(4):
                await self._connect(host, port)
                await self._send(
                    519,
                    options=properties(self._client_properties(access_token)),
                    data=properties({"CX_UNAME": "", "mode": "0"}),
                )
                response, login = await self._login_reply()
                redirect = _redirect(response, login, port)
                if redirect is None:
                    break
                if attempt == 3:
                    raise ProtocolError("Tend camera login redirected too many times")
                await self._close_socket()
                host, port = redirect
            if response.action == 1:
                raise AccessDenied("Tend camera login was rejected")
            self._sender, self._recipient = response.sender, response.recipient
            self._authenticated = True
            self._logged_in(login)

    async def _read_frame(self) -> Frame:
        if self._reader is None:
            raise CannotConnect("The Tend connection is not open")
        return await Frame.read(self._reader)

    async def start(self, device_alias: str, access_token: str) -> dict[str, Any]:
        """Authenticate and request the device's KVS connection options once."""
        if self._closed is not None:
            raise ProtocolError("A Tend viewer session cannot be reused")
        if not isinstance(access_token, str) or not access_token or len(access_token) > 16384:
            raise ProtocolError("Missing or invalid MyQ camera access token")
        self._device_alias = _alias(device_alias)
        self._closed = asyncio.get_running_loop().create_future()
        try:
            await self._login(access_token)
            async with asyncio.timeout(self._answer_timeout):
                self._requested = True
                await self._command(
                    self._require_video(self._user_alias), correlation=self._correlation
                )
                while True:
                    response = await self._read_frame()
                    messages = self._check_response(response)
                    for message in messages:
                        parts = [part.strip() for part in message.split("|")]
                        if (
                            len(parts) < 7
                            or parts[0] != "videomid"
                            or parts[4] != "accept-video-receive"
                        ):
                            continue
                        if parts[5] != self._device_alias or parts[6] != "12":
                            continue
                        try:
                            options = json.loads(parts[3])
                        except (ValueError, UnicodeError, RecursionError):
                            raise ProtocolError("Invalid Tend live video options") from None
                        if not isinstance(options, dict) or not isinstance(
                            options.get("kvs"), dict
                        ):
                            raise ProtocolError("Camera did not offer a supported WebRTC stream")
                        self._tasks = [
                            asyncio.create_task(self._listen()),
                            asyncio.create_task(self._keepalive()),
                        ]
                        return options
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, TimeoutError, UnicodeError, asyncio.IncompleteReadError):
            await self.close()
            raise CannotConnect("Tend camera signaling failed or timed out") from None
        except MyQError:
            await self.close()
            raise

    def _check_response(self, response: Frame) -> list[str]:
        # eg1/b turns actions 1 and 310 into a NAK; ConnectionManager drops the
        # connection only after more than MAX_NAKS consecutive NAKs.
        if response.action in (1, 310):
            self._naks += 1
            if self._naks > MAX_NAKS:
                raise AccessDenied("Tend camera connection was rejected or ended")
            return []
        self._naks = 0
        messages = _messages(response)
        for message in messages:
            parts = message.strip().split("|")
            if parts[0] == "videomid" and len(parts) >= 5:
                parts = parts[4:]
            if parts[0] == "kick-out":
                raise AccessDenied("Tend camera connection was denied or ended")
            if (
                parts[0] == "deny-video-receive"
                and len(parts) > 1
                and parts[1]
                and parts[1] in {self._device_alias, self._video_alias}
            ):
                error = denial(parts[2] if len(parts) > 2 else "")
                if isinstance(error, CameraWakeFailed):
                    raise error
                raise AccessDenied("Tend camera connection was denied or ended")
        return messages

    async def _listen(self) -> None:
        try:
            while True:
                for message in self._check_response(await self._read_frame()):
                    parts = message.strip().split("|")
                    # Native CommandHandler routes these messages by source alias.
                    # Treat them only as invalidations; REST remains authoritative.
                    if len(parts) >= 3 and parts[0] in {
                        "status",
                        "device-info",
                        "spydonk-device-info",
                        "ptz-status",
                    }:
                        self._status_changed(parts[1])
        except asyncio.CancelledError:
            raise
        except (AccessDenied, CameraWakeFailed) as error:
            self._fail(error)
        except (OSError, UnicodeError, asyncio.IncompleteReadError, MyQError):
            self._fail(CannotConnect("Tend camera signaling disconnected"))

    def _status_changed(self, alias: str) -> None:
        if alias == self._device_alias and self.on_status is not None:
            self.on_status()

    async def _keepalive(self) -> None:
        try:
            while True:
                await self._send(526)
                await asyncio.sleep(KEEPALIVE_INTERVAL)
        except asyncio.CancelledError:
            raise
        except (OSError, MyQError):
            self._fail(CannotConnect("Tend camera signaling disconnected"))

    def _fail(self, failure: MyQError) -> None:
        self._failure = failure
        if self._closed is not None and not self._closed.done():
            self._closed.set_result(None)

    async def wait_closed(self) -> None:
        """Wait for loss of signaling, allowing the viewer to stop its media peer."""
        if self._closed is not None:
            await asyncio.shield(self._closed)
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        """Send the observed stop/logout once and always close the TLS socket."""
        async with self._close_lock:
            await self._close()

    async def _close(self) -> None:
        self._closing = True
        self.on_status = None
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        writer = self._writer
        if writer is not None:
            try:
                async with asyncio.timeout(2):
                    if self._requested and self._user_alias:
                        writer.write(
                            Frame(
                                411,
                                self._sender,
                                self._recipient,
                                properties(
                                    {
                                        "CX_DSTID": self._device_alias,
                                        "CorrelationID": self._correlation,
                                    }
                                ),
                                (
                                    f"stop-video-receive|{self._video_alias or self._user_alias}|"
                                    f"{self._video_mode}|"
                                ).encode(),
                            ).encode()
                        )
                    if self._authenticated:
                        writer.write(
                            Frame(408, self._sender, self._recipient, struct.pack("<I", 0)).encode()
                        )
                    await writer.drain()
            except (OSError, TimeoutError):
                pass
            finally:
                await self._close_socket()
        if self._closed is not None and not self._closed.done():
            self._closed.set_result(None)

    async def _close_socket(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass


class TendStatusSession(TendSession):
    """Authenticated, read-only camera status feed without opening live media.

    ContactManager.onLoggedIn starts refresh_wait and keepalive before live view.
    In the TLS path refresh_wait is just a blocking frame read (eg1/b.t).
    The caller owns reconnection and pauses this feed while opening live sessions.
    """

    def __init__(self, aliases: set[str], on_status: Callable[[str], None]) -> None:
        super().__init__()
        self.update_aliases(aliases)
        self._on_status_alias = on_status

    def update_aliases(self, aliases: set[str]) -> None:
        if not aliases or len(aliases) > 4096:
            raise ProtocolError("Camera status subscription requires known camera aliases")
        self._aliases = frozenset(_alias(alias) for alias in aliases)

    async def subscribe(self, access_token: str) -> None:
        """Log in and listen for status pushes; viewers use start() instead."""
        if self._closed is not None:
            raise ProtocolError("A Tend status session cannot be reused")
        if not isinstance(access_token, str) or not access_token or len(access_token) > 16384:
            raise ProtocolError("Missing or invalid MyQ camera access token")
        self._closed = asyncio.get_running_loop().create_future()
        try:
            await self._login(access_token)
            self._tasks = [
                asyncio.create_task(self._listen()),
                asyncio.create_task(self._keepalive()),
            ]
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, TimeoutError, UnicodeError, asyncio.IncompleteReadError):
            await self.close()
            raise CannotConnect("Tend camera status connection failed or timed out") from None
        except MyQError:
            await self.close()
            raise

    def _status_changed(self, alias: str) -> None:
        if alias in self._aliases and not self._closing:
            self._on_status_alias(alias)


class TendCommandSession(TendSession):
    """A short login used only to send one command when no other session is open."""

    async def open(self, access_token: str) -> None:
        if self._closed is not None:
            raise ProtocolError("A Tend command session cannot be reused")
        if not isinstance(access_token, str) or not access_token or len(access_token) > 16384:
            raise ProtocolError("Missing or invalid MyQ camera access token")
        self._closed = asyncio.get_running_loop().create_future()
        try:
            await self._login(access_token)
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, TimeoutError, UnicodeError, asyncio.IncompleteReadError):
            await self.close()
            raise CannotConnect("Tend camera command connection failed or timed out") from None
        except MyQError:
            await self.close()
            raise

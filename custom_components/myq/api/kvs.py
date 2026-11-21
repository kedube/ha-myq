"""Kinesis signaling recovered from the Android mode-12 video player.

The Tend handshake supplies the signed URL and temporary ICE credentials. This
module neither mints AWS credentials nor sends MyQ OAuth credentials to AWS.
"""

import asyncio
import base64
import binascii
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

import aiohttp
from yarl import URL

from .errors import CannotConnect, MyQError, ProtocolError

MAX_MESSAGE = 256 * 1024
MAX_SDP = 128 * 1024
CONNECT_TIMEOUT = 20


def _text(value: Any, limit: int = 8192, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not value and not empty):
        raise ProtocolError("Invalid MyQ video signaling data")
    return value


def _aws_host(host: str | None, region: str) -> bool:
    return bool(
        host
        and re.fullmatch(
            rf"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.kinesisvideo\.{re.escape(region)}\.amazonaws\.com",
            host,
        )
    )


@dataclass(frozen=True)
class IceServer:
    """Short-lived connection material; never display credentials in diagnostics."""

    urls: tuple[str, ...] = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    ttl: int | None = None


@dataclass(frozen=True)
class KvsConfig:
    url: str = field(repr=False)
    region: str
    ice_servers: tuple[IceServer, ...] = field(repr=False)
    expires_at: float | None = field(default=None, repr=False)

    @classmethod
    def from_server_options(cls, options: str | dict) -> Self:
        """Parse accept-video-receive's serverOpt.kvs, not video-call metadata."""
        try:
            if isinstance(options, str):
                if len(options) > MAX_MESSAGE:
                    raise ValueError
                options = json.loads(options)
            if not isinstance(options, dict) or not isinstance(options.get("kvs"), dict):
                raise ValueError
            data = options["kvs"]
            region = _text(data.get("region"), 64)
            if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
                raise ValueError
            url = _text(data.get("channelUrl"), 16384)
            parsed = urlsplit(url)
            if (
                parsed.scheme != "wss"
                or not _aws_host(parsed.hostname, region)
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or any(ord(char) < 33 for char in url)
            ):
                raise ValueError
            # Lifetimes count from receipt on the monotonic clock: the app applies no
            # expiry check, and HA's wall clock may differ from AWS's.
            now = time.monotonic()
            expiry = None
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=50)
            if "X-Amz-Date" in query or "X-Amz-Expires" in query:
                dates, durations = query.get("X-Amz-Date", []), query.get("X-Amz-Expires", [])
                if len(dates) != 1 or len(durations) != 1:
                    raise ValueError
                datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                duration = int(durations[0])
                if duration <= 0:
                    raise ValueError
                expiry = now + duration

            servers = data.get("iceServers")
            if not isinstance(servers, list) or not 1 <= len(servers) <= 16:
                raise ValueError
            ice = []
            for server in servers:
                if not isinstance(server, dict):
                    raise ValueError
                urls = server.get("urls")
                if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
                    raise ValueError
                for value in urls:
                    uri = _text(value, 2048)
                    scheme, separator, address = uri.partition(":")
                    address_parts = urlsplit("//" + address)
                    if (
                        not separator
                        or scheme not in ("stun", "turn", "turns")
                        or not _aws_host(address_parts.hostname, region)
                        or address_parts.username is not None
                        or address_parts.password is not None
                        or address_parts.path
                        or address_parts.fragment
                        or address_parts.port not in (None, 443, 3478, 5349)
                        or address_parts.query not in ("", "transport=udp", "transport=tcp")
                    ):
                        raise ValueError
                username = _text(server.get("username", ""), empty=True)
                password = _text(server.get("password", ""), empty=True)
                if any(value.startswith(("turn:", "turns:")) for value in urls) and (
                    not username or not password
                ):
                    raise ValueError
                ttl = server.get("ttl")
                if ttl is not None:
                    if type(ttl) is not int or ttl <= 0:
                        raise ValueError
                    expiry = min(expiry, now + ttl) if expiry is not None else now + ttl
                ice.append(IceServer(tuple(urls), username, password, ttl))
            result = cls(url, region, tuple(ice), expiry)
            result.ensure_fresh()
            return result
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ProtocolError("Invalid MyQ video connection configuration") from None

    def ensure_fresh(self) -> None:
        if self.expires_at is not None and self.expires_at <= time.monotonic() + 5:
            raise ProtocolError("MyQ video connection credentials have expired")


def _decode_payload(encoded: str) -> bytes:
    """WebSocketMessageParser decodes leniently: line breaks, missing padding and
    either Base64 alphabet are accepted. Anything else is still rejected."""
    cleaned = "".join(encoded.split()).replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    return base64.b64decode(cleaned, validate=True)


def _candidate(data: dict) -> dict:
    # An empty candidate marks the end of the camera's candidates.
    candidate = _text(data.get("candidate", ""), 8192, empty=True)
    mid = data.get("sdpMid")
    if mid is not None:
        _text(mid, 256, empty=True)
    index = data.get("sdpMLineIndex")
    if index is not None and (type(index) is not int or not 0 <= index <= 255):
        raise ProtocolError("Invalid MyQ video ICE candidate")
    if mid is None and index is None:
        raise ProtocolError("Invalid MyQ video ICE candidate")
    return {"candidate": candidate, "sdpMid": mid, "sdpMLineIndex": index}


def _event(message: str) -> dict | None:
    """Decode the AWS envelope without surfacing server bodies or peer details."""
    try:
        if len(message) > MAX_MESSAGE:
            raise ValueError
        envelope = json.loads(message)
        if not isinstance(envelope, dict):
            raise ValueError
        kind = envelope.get("messageType")
        if kind in ("GO_AWAY", "RECONNECT_ICE_SERVER"):
            raise CannotConnect("MyQ video signaling requires a new session")
        if kind == "STATUS_RESPONSE":
            raise ProtocolError("MyQ video signaling request was rejected")
        if kind not in ("SDP_ANSWER", "ICE_CANDIDATE"):
            return None
        encoded = envelope.get("messagePayload")
        if not encoded:
            return None  # KvsEventParser ignores messages without a payload.
        payload = json.loads(_decode_payload(_text(encoded, MAX_MESSAGE)))
        if not isinstance(payload, dict):
            raise ValueError
        if kind == "SDP_ANSWER":
            if payload.get("type") != "answer":
                raise ValueError
            return {"type": "answer", "sdp": _text(payload.get("sdp"), MAX_SDP)}
        return {"type": "candidate", **_candidate(payload)}
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise ProtocolError("Invalid MyQ video signaling message") from None


class KvsSession:
    """One viewer signaling session, with no automatic reconnect or replay."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: KvsConfig,
        callback: Callable[[dict], Awaitable[None]],
        on_error: Callable[[MyQError], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self._shared_session = session
        self._callback = callback
        self._on_error = on_error
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._closed = False
        self._client_id = f"session.{secrets.randbelow(100)}"

    async def open(self) -> None:
        if self._closed or self._session is not None:
            raise CannotConnect("MyQ video signaling session cannot be reopened")
        self.config.ensure_fresh()
        attempted = False

        async def guard(request, handler):
            nonlocal attempted
            # aiohttp's websocket API has no allow_redirects argument. Reject the
            # redirect response before its HTTP client can follow Location.
            if str(request.url) != self.config.url:
                raise ProtocolError("MyQ video signaling URL changed")
            if attempted:
                raise CannotConnect("MyQ video signaling handshake replay was blocked")
            attempted = True
            response = await handler(request)
            if response.status != 101:
                response.close()
                raise CannotConnect("MyQ video signaling handshake was rejected")
            return response

        try:
            connector = self._shared_session.connector
            if connector is None or connector.closed:
                raise CannotConnect("MyQ video transport is unavailable")
            self._session = aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
                middlewares=(guard,),
            )
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self._ws = await self._session.ws_connect(
                    URL(self.config.url, encoded=True),
                    timeout=aiohttp.ClientWSTimeout(ws_close=5),
                    max_msg_size=MAX_MESSAGE,
                    autoping=True,
                )
            if self._closed:
                raise CannotConnect("MyQ video signaling session was closed")
            self._reader = asyncio.create_task(self._read(), name="myq-kvs-signaling")
        except asyncio.CancelledError:
            await self.close()
            raise
        except MyQError:
            await self.close()
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            await self.close()
            raise CannotConnect("Cannot connect to MyQ video signaling") from None

    async def _send(self, action: str, payload: dict) -> None:
        if self._closed or self._ws is None or self._ws.closed:
            raise CannotConnect("MyQ video signaling session is closed")
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await self._ws.send_json(
                    {
                        "action": action,
                        "messagePayload": encoded,
                        "senderClientId": self._client_id,
                        "recipientClientId": "",
                    }
                )
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise CannotConnect("Could not send MyQ video signaling message") from None

    async def send_offer(self, sdp: str) -> None:
        await self._send("SDP_OFFER", {"type": "offer", "sdp": _text(sdp, MAX_SDP)})

    async def send_candidate(self, candidate: dict) -> None:
        await self._send("ICE_CANDIDATE", _candidate(candidate))

    async def _read(self) -> None:
        error: MyQError | None = None
        try:
            assert self._ws is not None
            async for message in self._ws:
                if message.type is aiohttp.WSMsgType.TEXT:
                    event = _event(message.data)
                    if event is not None:
                        await self._callback(event)
                else:
                    raise CannotConnect("MyQ video signaling connection ended")
            if not self._closed:
                error = CannotConnect("MyQ video signaling connection ended")
        except asyncio.CancelledError:
            raise
        except MyQError as err:
            error = err
        except Exception:
            error = CannotConnect("MyQ video signaling failed")
        finally:
            await self.close()
        if error is not None and self._on_error is not None:
            await self._on_error(error)

    async def close(self) -> None:
        self._closed = True
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        ws, self._ws = self._ws, None
        session, self._session = self._session, None
        try:
            if ws is not None:
                async with asyncio.timeout(5):
                    await ws.close()
        except (aiohttp.ClientError, TimeoutError, OSError):
            pass
        finally:
            if session is not None:
                await session.close()

"""Tend mode-9 negotiation using the integration's existing TLS signaling.

Only an exact camera alias from the authenticated inventory can open media. The
inventory's temporary AES material stays inside this session, never in entity data.
"""

import asyncio
import ipaddress
import re
from typing import Any

from .errors import AccessDenied, CannotConnect, MyQError, ProtocolError
from .tend import Frame, TendSession, _alias
from .tend_rtp import TendRtp

OFFER_TIMEOUT = 15


def _camera_registration(inventory: str, destination: str) -> tuple[str, str, bytes]:
    """Select one authenticated destination without falling back to another camera."""
    matches = []
    for row in inventory.splitlines():
        identity, separator, fields = row.partition(",")
        if separator and identity == destination:
            matches.append(fields)
    if len(matches) != 1:
        raise ProtocolError("The requested camera was not uniquely listed by Tend")
    owner, separator, camera = destination.partition(":")
    if not separator or not camera or not owner.isascii() or not 1 <= len(owner) <= 256:
        raise ProtocolError("Invalid Tend legacy camera identity")
    values = {}
    for field in matches[0].split("+"):
        name, separator, value = field.partition("=")
        if separator:
            if name in values:
                raise ProtocolError("Ambiguous Tend camera registration")
            values[name] = value
    key = values.get("AES", "")
    if key and (len(key) != 16 or not key.isascii()):
        raise ProtocolError("Invalid Tend camera media registration")
    local = values.get("LIP", "")
    if local:
        try:
            address = ipaddress.IPv4Address(local)
        except ValueError:
            raise ProtocolError("Invalid Tend camera address") from None
        if address.is_multicast or address.is_loopback or address.is_unspecified:
            raise ProtocolError("Invalid Tend camera address")
    return owner, local, key.encode("ascii")


def _relay(metadata: bytes) -> tuple[str, int]:
    """Read the single RTP endpoint returned over the authenticated TLS channel."""
    endpoints = re.findall(rb"rtp://([a-zA-Z0-9.-]+):([0-9]+)", metadata)
    if len(endpoints) != 1:
        raise ProtocolError("Tend did not provide one legacy video relay")
    host_bytes, port_bytes = endpoints[0]
    host = host_bytes.decode("ascii")
    if (
        len(host) > 253
        or any(
            not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
            for part in host.split(".")
        )
        or len(port_bytes) > 5
        or not 1 <= int(port_bytes) <= 65535
    ):
        raise ProtocolError("Invalid Tend video relay")
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        pass
    else:
        if address.is_multicast or address.is_unspecified or address.is_loopback:
            raise ProtocolError("Invalid Tend video relay")
    return host, int(port_bytes)


class LegacyTendSession(TendSession):
    """A mode-9 video session with paired UDP media and control channels."""

    def __init__(self) -> None:
        super().__init__()
        self._video_mode = 9
        self._media: TendRtp | None = None
        self._legacy_cleanup: asyncio.Task | None = None

    def _offer(self, response: Frame) -> tuple[str, int] | None:
        for message in self._check_response(response):
            fields = message.strip().split("|")
            metadata = response.options
            if fields[0] == "videomid" and len(fields) >= 7:
                metadata = fields[3].encode("utf-8")
                fields = fields[4:]
            if len(fields) < 2 or fields[1] not in {self._device_alias, self._video_alias}:
                continue
            if fields[0] == "deny-video-receive":
                raise AccessDenied("The camera declined legacy live video")
            if len(fields) >= 3 and fields[0] == "accept-video-receive" and fields[2] == "9":
                return _relay(metadata)
        return None

    async def start(self, device_alias: str, access_token: str) -> dict[str, Any]:
        if self._closed is not None:
            raise ProtocolError("A Tend viewer session cannot be reused")
        if not isinstance(access_token, str) or not 1 <= len(access_token) <= 16384:
            raise ProtocolError("Missing or invalid MyQ camera access token")
        self._device_alias = _alias(device_alias)
        self._closed = asyncio.get_running_loop().create_future()
        try:
            await self._login(access_token)
            self._video_alias, camera_ip, key = _camera_registration(
                self._login_inventory, self._device_alias
            )
            self._login_inventory = ""
            async with asyncio.timeout(OFFER_TIMEOUT):
                self._requested = True
                await self._command(
                    f"require-video-send|{self._video_alias}|9|", correlation=self._correlation
                )
                while (relay := self._offer(await self._read_frame())) is None:
                    pass
            self._media = TendRtp()
            # Keep signaling alive throughout the UDP rendezvous, but never replay
            # require/start commands on a failure: a new view gets a new session.
            self._tasks = [
                asyncio.create_task(self._listen()),
                asyncio.create_task(self._keepalive()),
            ]
            await self._media.start(relay, self._video_alias, camera_ip, key)
            if self._closed.done():
                await self.wait_closed()
                raise CannotConnect("Tend signaling ended while opening video")
            async with asyncio.timeout(OFFER_TIMEOUT):
                await self._command(
                    f"start-video-receive|{self._video_alias}|9|", correlation=self._correlation
                )
                # Session quality settings; SAVE=0 leaves camera preferences intact.
                await self._command(
                    f"modify-device|{self._video_alias}|QTY=3+FPS=20+SIZE=0+SAVE=0|"
                )
            return {}
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, TimeoutError, UnicodeError, asyncio.IncompleteReadError):
            await self.close()
            raise CannotConnect("Tend legacy video failed or timed out") from None
        except MyQError:
            await self.close()
            raise

    async def recv(self) -> bytes:
        if self._media is None or not self.connected:
            raise CannotConnect("Tend legacy video is not connected")
        return await self._media.recv()

    async def close(self) -> None:
        """Finish UDP and TLS teardown even if the requesting viewer is cancelled."""
        if self._legacy_cleanup is None:
            self._legacy_cleanup = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._legacy_cleanup)

    async def _shutdown(self) -> None:
        try:
            if self._media is not None:
                await self._media.close()
        finally:
            self._media = None
            self._login_inventory = ""
            await super().close()

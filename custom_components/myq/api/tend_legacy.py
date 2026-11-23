"""Tend mode-9 video negotiation for TC cameras over the authenticated TLS signaling.

The exchange follows clients known to work with these cameras. Where they are lenient
this is too: the broker's pushes are matched by their leading command text, and the
camera's own network address and encryption key come from the login's device list.
That material stays inside the session; none of it reaches entity data or logs.
"""

import asyncio
import ipaddress
import re
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from .errors import AccessDenied, CannotConnect, MyQError, ProtocolError
from .tend import HANDSHAKE_TIMEOUT, Frame, TendSession, _alias, parse_properties
from .tend_rtp import TendRtp

OFFER_TIMEOUT = 15
# Frames read while waiting for the login's answer; a broker may push others first.
LOGIN_FRAMES = 16
RETRY_DELAY = 4
KEEPALIVE_INTERVAL = 20
_STATUS = (b"status|", b"device-info|", b"spydonk-device-info|", b"ptz-status|")


def _camera_registration(
    inventory: str, destination: str, openers: Iterable[str] = ()
) -> tuple[str, str, str, bytes]:
    """Select one listed camera: (destination, account alias, camera address, AES key).

    A row is "<account alias>:<camera>,NAME=value+NAME=value". The camera's own alias
    selects its row; failing that, the row naming one of its openers (GSN) does. Zero
    or several matches never fall back to another camera.
    """
    rows = []
    # Only real line ends separate rows: a camera name may hold other separators.
    for line in re.split(r"\r\n|\r|\n", inventory):
        identity, separator, fields = line.partition(",")
        if separator and ":" in identity:
            values = dict(item.split("=", 1) for item in fields.split("+") if "=" in item)
            rows.append((identity, values))
    matches = [row for row in rows if row[0] == destination]
    if not matches:
        serials = {serial for serial in openers if serial}
        matches = [row for row in rows if row[1].get("GSN") in serials]
    if len(matches) != 1:
        raise ProtocolError(
            f"The requested camera was not uniquely listed by Tend ({len(rows)} listed)"
        )
    identity, values = matches[0]
    owner = identity.partition(":")[0]
    if not owner.isascii() or not 1 <= len(owner) <= 256:
        raise ProtocolError("Invalid Tend legacy camera identity")
    key = values.get("AES", "")
    if key and (len(key) != 16 or not key.isascii()):
        raise ProtocolError("Invalid Tend camera media registration")
    try:
        local = str(ipaddress.IPv4Address(values.get("LIP", "")))
    except ValueError:
        local = ""  # Only a preference among the addresses the relay announces.
    return _alias(identity), owner, local, key.encode("ascii")


def _relay(*sources: bytes) -> tuple[str, int]:
    """The first RTP endpoint named in an acceptance's metadata."""
    for source in sources:
        # Java properties escape colons; the endpoint is matched either way.
        match = re.search(rb"rtp://([a-zA-Z0-9.-]+):([0-9]{1,5})", source.replace(b"\\", b""))
        if match is None:
            continue
        host, port = match[1].decode("ascii"), int(match[2])
        if (
            len(host) > 253
            or not 1 <= port <= 65535
            or any(
                not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
                for part in host.split(".")
            )
        ):
            break
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            return host, port
        if (
            address.is_multicast
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            break
        return host, port
    raise ProtocolError("Tend did not provide a legacy video relay")


def _texts(frame: Frame) -> list[bytes]:
    """Command texts in a frame, kept as bytes: not every broker push is text."""
    if frame.action != 501:
        return [frame.data] if frame.data else []
    texts = []
    offset = 0
    while len(frame.data) - offset >= 5:
        kind = frame.data[offset]
        length = int.from_bytes(frame.data[offset + 1 : offset + 5], "little")
        offset += 5
        if kind not in (0, 1) or length > len(frame.data) - offset:
            break
        if kind == 0:
            texts.append(frame.data[offset : offset + length])
        offset += length
    return texts


class LegacyTendSession(TendSession):
    """A mode-9 video session: TLS signaling plus paired UDP media and control ports.

    ``on_video`` receives each encoded H.264 record as the camera sent it.
    """

    def __init__(self, openers: Iterable[str] = ()) -> None:
        super().__init__()
        self._video_mode = 9
        self._openers = frozenset(openers)
        self._inventory = ""
        self._media: TendRtp | None = None
        self._offer: asyncio.Future[tuple[str, int]] | None = None
        self._legacy_cleanup: asyncio.Task | None = None
        self.on_video: Callable[[bytes], None] | None = None

    @property
    def media(self) -> TendRtp | None:
        return self._media

    def _client_properties(self, access_token: str) -> dict[str, str]:
        # The client profile TC cameras are known to answer for mode 9. The app's own
        # profile (SDK 6) belongs to its KVS WebRTC negotiation.
        return {
            "partner-id": "myQ",
            "CX_UNAME": "",
            "client_type": "Python myqcam",
            "sdk_version": "2.1.0.56",
            "CX_PASSWD": "myQ.accessToken:" + access_token,
            "current_version": "5.243.1.73243",
            "os_name": "Python",
            "app-id": "com.chamberlain.android.liftmaster.myq",
            "os_version": "device-free",
        }

    async def _login_reply(self) -> tuple[Frame, dict[str, str]]:
        # Read on until the answer is a rejection, a redirect or carries a session.
        for _ in range(LOGIN_FRAMES):
            response = await self._read_frame()
            if response.action not in (1, 519):
                continue
            login = parse_properties(response.data)
            if response.action == 1 or login.keys() & {
                "sid",
                "session-id",
                "alias",
                "device-info-list",
                "redirectHost",
                "1",
            }:
                return response, login
        raise ProtocolError("Tend broker did not return a session")

    def _logged_in(self, login: dict[str, str]) -> None:
        alias = login.get("alias")
        self._user_alias = _alias(alias) if alias else ""
        self._inventory = login.get("device-info-list", "")
        # Working clients leave the header's routing field zero after login.
        self._recipient = 0

    def _video(self, record: bytes) -> None:
        if self.on_video is not None and not self._closing:
            self.on_video(record)

    def _fail(self, failure: MyQError) -> None:
        super()._fail(failure)
        if self._offer is not None and not self._offer.done():
            self._offer.set_exception(failure)

    def _message(self, frame: Frame, text: bytes) -> None:
        if text.startswith(b"accept-video-receive|"):
            if self._offer is not None and not self._offer.done():
                try:
                    self._offer.set_result(_relay(frame.options, text))
                except ProtocolError as error:
                    self._offer.set_exception(error)
        elif text.startswith(b"kick-out"):
            raise AccessDenied("Tend camera connection was denied or ended")
        elif text.startswith(b"deny-video-receive|"):
            target = text.split(b"|", 2)[1].decode("utf-8", "replace")
            if target in {self._device_alias, self._video_alias}:
                raise AccessDenied("The camera declined live video")
        elif text.startswith(_STATUS):
            parts = text.split(b"|", 2)
            if len(parts) == 3:
                self._status_changed(parts[1].decode("utf-8", "replace"))

    async def _listen(self) -> None:
        try:
            while True:
                frame = await self._read_frame()
                for text in _texts(frame):
                    self._message(frame, text)
        except asyncio.CancelledError:
            raise
        except AccessDenied as error:
            self._fail(error)
        except (OSError, asyncio.IncompleteReadError, MyQError):
            self._fail(CannotConnect("Tend camera signaling disconnected"))

    async def _keepalive(self) -> None:
        # Working clients send the first keepalive after one interval, not at once.
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                await self._send(526)
        except asyncio.CancelledError:
            raise
        except (OSError, MyQError):
            self._fail(CannotConnect("Tend camera signaling disconnected"))

    async def _request_video(self) -> tuple[str, int]:
        """Ask the camera to send video; its acceptance names the relay to register with."""
        assert self._closed is not None
        if self._closed.done():
            raise self._failure or CannotConnect("Tend signaling ended while opening video")
        offer = self._offer = asyncio.get_running_loop().create_future()
        try:
            self._requested = True
            self._correlation = "LOG-" + uuid.uuid4().hex
            await self._command(
                f"require-video-send|{self._video_alias}|9|", correlation=self._correlation
            )
            async with asyncio.timeout(OFFER_TIMEOUT):
                return await offer
        finally:
            self._offer = None
            if offer.done() and not offer.cancelled():
                offer.exception()  # Retrieved here when the wait itself was interrupted.

    async def _stop_media(self) -> None:
        """Give up one rendezvous attempt, telling the camera to stop first."""
        media, self._media = self._media, None
        try:
            async with asyncio.timeout(1):
                await self._command(
                    f"stop-video-receive|{self._video_alias}|9|", correlation=self._correlation
                )
        except (OSError, TimeoutError, MyQError):
            pass
        finally:
            if media is not None:
                await media.close()

    async def start(self, device_alias: str, access_token: str) -> dict[str, Any]:
        if self._closed is not None:
            raise ProtocolError("A Tend viewer session cannot be reused")
        if not isinstance(access_token, str) or not 1 <= len(access_token) <= 16384:
            raise ProtocolError("Missing or invalid MyQ camera access token")
        self._device_alias = _alias(device_alias)
        self._closed = asyncio.get_running_loop().create_future()
        try:
            await self._login(access_token)
            self._device_alias, self._video_alias, camera_ip, key = _camera_registration(
                self._inventory, self._device_alias, self._openers
            )
            self._inventory = ""
            self._user_alias = self._user_alias or self._video_alias
            # Signaling is read and kept alive from here on, through the UDP rendezvous.
            self._tasks = [
                asyncio.create_task(self._listen()),
                asyncio.create_task(self._keepalive()),
            ]
            for attempt in range(2):
                relay = await self._request_video()
                self._media = TendRtp(self._video, self._fail)
                try:
                    await self._media.start(relay, self._video_alias, camera_ip, key)
                    break
                except CannotConnect:
                    # The first rendezvous can miss; one fresh request follows a pause.
                    await self._stop_media()
                    if attempt:
                        raise
                    await asyncio.sleep(RETRY_DELAY)
            if self._closed.done():
                await self.wait_closed()
                raise CannotConnect("Tend signaling ended while opening video")
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
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
            self._inventory = ""
            await super().close()

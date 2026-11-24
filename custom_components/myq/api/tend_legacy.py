"""Tend video negotiation for TC cameras over the authenticated TLS signaling.

The exchange follows clients known to work with these cameras. Where they are lenient
this is too: the broker's pushes are matched by their leading command text, and the
camera's own network address and encryption key come from the login's device list.
That material stays inside the session; none of it reaches entity data or logs.

Video modes are tried in the app's order (MediaConnectionManager): UDP to the camera
(mode 9 with its AES key, 8 without), UDP through Tend's relay (10, or 11 for a camera
listing RTP11=1; only for a camera listing RTP=1), then Tend's TLS video channel (0).

As in the app (VideoCallManager.connect), a live view opens the camera's audio
connection alongside its video: the camera's microphone arrives on it, and talkback
is sent over it.
"""

import asyncio
import ipaddress
import re
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from .errors import AccessDenied, CameraWakeFailed, CannotConnect, MyQError, ProtocolError
from .live_audio import CODEC_NAMES
from .tend import (
    HANDSHAKE_TIMEOUT,
    WAKEUP_ANSWER_TIMEOUT,
    Frame,
    TendSession,
    _alias,
    denial,
    parse_properties,
)
from .tend_channel import JOIN_AUDIO, TendChannel, channel_address
from .tend_rtp import TendRtp

# The camera's audio frames: Tend's codec number, the audio, and the RTP sequence
# number (None over the TLS channel).
AudioCallback = Callable[[int, bytes, int | None], None]

OFFER_TIMEOUT = 15
# Frames read while waiting for the login's answer; a broker may push others first.
LOGIN_FRAMES = 16
RETRY_DELAY = 4
KEEPALIVE_INTERVAL = 20
CHANNEL_MODE = 0
RTP_MODES = frozenset({8, 9, 10, 11})
_CODEC = re.compile(r"[A-Za-z0-9_.-]{1,24}")
_STATUS = (b"status|", b"device-info|", b"spydonk-device-info|", b"ptz-status|")


def _camera_row(
    inventory: str, destination: str, openers: Iterable[str] = ()
) -> tuple[str, dict[str, str]]:
    """Select one listed camera's row: (identity, NAME=value fields).

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
    return matches[0]


def _registration(identity: str, values: dict[str, str]) -> tuple[str, str, str, bytes]:
    """A camera row's (destination, account alias, camera address, AES key)."""
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


def _camera_registration(
    inventory: str, destination: str, openers: Iterable[str] = ()
) -> tuple[str, str, str, bytes]:
    """Select one listed camera: (destination, account alias, camera address, AES key)."""
    return _registration(*_camera_row(inventory, destination, openers))


def _modes(values: dict[str, str], key: bytes, channel: bool, skip: frozenset[int]) -> list[int]:
    """The video modes to try, in order. The first UDP mode is tried twice: its first
    rendezvous can miss. Modes MyQ lists as failing for the camera are skipped, unless
    that would leave none."""
    direct = 9 if key else 8
    modes = [direct, direct]
    if values.get("RTP") == "1":
        modes.append(11 if values.get("RTP11") == "1" else 10)
    if channel:
        modes.append(CHANNEL_MODE)
    return [mode for mode in modes if mode not in skip] or modes


def _answers(text: bytes, mode: int) -> bool:
    """An acceptance for the requested mode; one that names no mode is taken as such."""
    parts = text.split(b"|")
    named = parts[2] if len(parts) > 2 else b""
    return not (named.isascii() and named.isdigit()) or int(named) == mode


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
    """A TC camera video session: TLS signaling plus UDP ports or a TLS video channel.

    ``on_video`` receives each encoded H.264 record as the camera sent it.
    """

    def __init__(self, openers: Iterable[str] = ()) -> None:
        super().__init__()
        self._video_mode = 9
        self._openers = frozenset(openers)
        self._inventory = ""
        self._media: TendRtp | TendChannel | None = None
        self._offer: asyncio.Future[tuple[str, int] | None] | None = None
        self._audio_offer: asyncio.Future[tuple[str, int] | None] | None = None
        self._audio_mode = 9
        self._legacy_cleanup: asyncio.Task | None = None
        self._login_recipient = 0
        self._video_channel: tuple[str, int] | None = None
        self._audio_channel: tuple[str, int] | None = None
        self._row: dict[str, str] = {}
        self._camera_ip = ""
        self._key = b""
        self._tried: list[int] = []
        self.on_video: Callable[[bytes], None] | None = None
        self.on_audio: AudioCallback | None = None
        # Open the camera's audio connection along with its video, as a live view does.
        self.listen = False
        self.audio_error: str | None = None
        self._audio_link: AudioLink | None = None
        self._audio_task: asyncio.Task | None = None
        # One audio request at a time: the camera's answer names no request.
        self._audio_lock = asyncio.Lock()
        self._audio_heard: set[int] = set()
        # Modes MyQ lists as failing for this camera (enable_connection_mode_override).
        self.skip_modes: frozenset[int] = frozenset()

    @property
    def media(self) -> TendRtp | TendChannel | None:
        return self._media

    @property
    def speaker_codecs(self) -> list[str]:
        """The talkback codecs the camera's row lists (SPKCDLIST, "opus:g711")."""
        listed = self._row.get("SPKCDLIST", "")
        return sorted(
            {name.strip().lower() for name in listed.split(":") if _CODEC.fullmatch(name.strip())}
        )[:8]

    @property
    def transport(self) -> dict[str, Any]:
        """How video and audio were reached, for diagnostics: no addresses."""
        link = self._audio_link
        return {
            "video_mode": self._video_mode if self._media is not None else None,
            "modes_tried": list(self._tried),
            "modes_skipped": sorted(self.skip_modes),
            "camera_lists_rtp": self._row.get("RTP") == "1",
            "camera_lists_rtp11": self._row.get("RTP11") == "1",
            "camera_has_key": bool(self._key),
            "video_channel_offered": self._video_channel is not None,
            "speaker_codecs_listed": self.speaker_codecs if self._row else None,
            "wakeup": self.wakeup,
            "audio_mode": link.mode if link is not None else None,
            "audio_frames": link.frames if link is not None else 0,
            "audio_codecs": sorted(CODEC_NAMES.get(code, str(code)) for code in self._audio_heard),
            "audio_error": self.audio_error,
        }

    @property
    def audio_pending(self) -> bool:
        """The camera's audio was asked for and has neither arrived nor failed."""
        return self.listen and self.audio_error is None and not self._audio_heard

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
        # The video and audio channels (mode 0) are joined with the login's identity.
        self._video_channel = channel_address(login, "video")
        self._audio_channel = channel_address(login, "audio")
        self._login_recipient = self._recipient
        # Working clients leave the header's routing field zero after login.
        self._recipient = 0

    def _video(self, record: bytes) -> None:
        if self.on_video is not None and not self._closing:
            self.on_video(record)

    def _audio(self, codec: int, data: bytes, sequence: int | None) -> None:
        if len(self._audio_heard) < 8:
            self._audio_heard.add(codec)
        if self.on_audio is not None and not self._closing:
            self.on_audio(codec, data, sequence)

    def _fail(self, failure: MyQError) -> None:
        super()._fail(failure)
        for offer in (self._offer, self._audio_offer):
            if offer is not None and not offer.done():
                offer.set_exception(failure)

    @staticmethod
    def _accepted(
        offer: asyncio.Future[tuple[str, int] | None] | None,
        mode: int,
        frame: Frame,
        text: bytes,
    ) -> None:
        if offer is None or offer.done() or not _answers(text, mode):
            return
        if mode not in RTP_MODES:
            offer.set_result(None)  # The channel comes from the login, not the answer.
            return
        try:
            offer.set_result(_relay(frame.options, text))
        except ProtocolError as error:
            offer.set_exception(error)

    def _message(self, frame: Frame, text: bytes) -> None:
        if text.startswith(b"accept-video-receive|"):
            self._accepted(self._offer, self._video_mode, frame, text)
        elif text.startswith(b"accept-audio-receive|"):
            # accept-audio-receive|<camera>|<session id>|<mode>|: the mode is the fourth.
            parts = text.split(b"|", 2)
            rest = parts[2] if len(parts) > 2 else b""
            self._accepted(self._audio_offer, self._audio_mode, frame, b"|" + rest)
        elif text.startswith(b"kick-out"):
            raise AccessDenied("Tend camera connection was denied or ended")
        elif text.startswith((b"deny-video-receive|", b"deny-audio-receive|")):
            parts = text.split(b"|", 3)
            target = parts[1].decode("utf-8", "replace")
            reason = parts[2].decode("utf-8", "replace") if len(parts) > 2 else ""
            if target not in {self._device_alias, self._video_alias}:
                return
            if text.startswith(b"deny-audio-receive|"):
                error = denial(reason)
                if self._audio_offer is not None and not self._audio_offer.done():
                    self._audio_offer.set_exception(
                        error
                        if isinstance(error, CameraWakeFailed)
                        else AccessDenied("The camera declined its audio connection")
                    )
                return
            raise denial(reason)
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
        except (AccessDenied, CameraWakeFailed) as error:
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

    def _check_open(self) -> None:
        assert self._closed is not None
        if self._closed.done():
            raise self._failure or CannotConnect("Tend signaling ended while opening video")

    async def _request_video(self, mode: int) -> tuple[str, int] | None:
        """Ask the camera to send video in one mode.

        An RTP mode's acceptance names the relay to register with; mode 0 uses the
        login's video channel instead, and its acceptance carries nothing needed.
        """
        self._check_open()
        offer = self._offer = asyncio.get_running_loop().create_future()
        try:
            self._requested = True
            self._video_mode = mode
            self._tried.append(mode)
            self._correlation = "LOG-" + uuid.uuid4().hex
            await self._command(
                self._require_video(self._video_alias), correlation=self._correlation
            )
            # A camera that may be asleep gets longer to answer.
            limit = WAKEUP_ANSWER_TIMEOUT if self.camera_asleep else OFFER_TIMEOUT
            async with asyncio.timeout(limit):
                return await offer
        finally:
            self._offer = None
            if offer.done() and not offer.cancelled():
                offer.exception()  # Retrieved here when the wait itself was interrupted.

    async def _stop_media(self) -> None:
        """Give up one attempt, telling the camera to stop first."""
        media, self._media = self._media, None
        try:
            async with asyncio.timeout(1):
                await self._command(
                    f"stop-video-receive|{self._video_alias}|{self._video_mode}|",
                    correlation=self._correlation,
                )
        except (OSError, TimeoutError, MyQError):
            pass
        finally:
            if media is not None:
                await media.close()

    async def _open_media(self, mode: int, relay: tuple[str, int] | None) -> None:
        if mode == CHANNEL_MODE:
            assert self._video_channel is not None
            # The app asks the camera to start as soon as it accepts, then joins.
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                await self._command(
                    f"start-video-receive|{self._video_alias}|{mode}|",
                    correlation=self._correlation,
                )
            channel = self._media = TendChannel(self._video, self._fail)
            await channel.start(
                self._video_channel,
                self._sender,
                self._login_recipient,
                {self._device_alias, self._video_alias},
            )
            return
        assert relay is not None
        rtp = self._media = TendRtp(self._video, self._fail)
        await rtp.start(relay, self._video_alias, self._camera_ip, self._key)

    async def connect(self, device_alias: str, access_token: str) -> None:
        """Log in and select the camera, keeping signaling open without any video."""
        if self._closed is not None:
            raise ProtocolError("A Tend viewer session cannot be reused")
        if not isinstance(access_token, str) or not 1 <= len(access_token) <= 16384:
            raise ProtocolError("Missing or invalid MyQ camera access token")
        self._device_alias = _alias(device_alias)
        self._closed = asyncio.get_running_loop().create_future()
        await self._login(access_token)
        identity, self._row = _camera_row(self._inventory, self._device_alias, self._openers)
        self._device_alias, self._video_alias, self._camera_ip, self._key = _registration(
            identity, self._row
        )
        self._inventory = ""
        self._user_alias = self._user_alias or self._video_alias
        # Signaling is read and kept alive from here on, through the media setup.
        self._tasks = [
            asyncio.create_task(self._listen()),
            asyncio.create_task(self._keepalive()),
        ]

    async def _start_video(self) -> None:
        modes = _modes(self._row, self._key, self._video_channel is not None, self.skip_modes)
        failure: MyQError | None = None
        missed = False
        for index, mode in enumerate(modes):
            if index and modes[index - 1] == mode:
                if not missed:
                    continue  # A repeat only follows a missed rendezvous.
                await asyncio.sleep(RETRY_DELAY)
            try:
                relay = await self._request_video(mode)
            except TimeoutError:
                await self._stop_media()
                failure, missed = (
                    CannotConnect("The camera did not answer the video request"),
                    False,
                )
                continue
            try:
                await self._open_media(mode, relay)
            except (CannotConnect, TimeoutError) as error:
                if isinstance(error, CameraWakeFailed):
                    raise
                await self._stop_media()
                failure = (
                    error
                    if isinstance(error, CannotConnect)
                    else CannotConnect("Tend video channel timed out")
                )
                missed = True
                continue
            break
        else:
            raise failure or CannotConnect("Tend legacy video failed or timed out")
        if self._closed is not None and self._closed.done():
            await self.wait_closed()
            raise CannotConnect("Tend signaling ended while opening video")
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            if self._video_mode != CHANNEL_MODE:
                await self._command(
                    f"start-video-receive|{self._video_alias}|{self._video_mode}|",
                    correlation=self._correlation,
                )
            # Session quality settings; SAVE=0 leaves camera preferences intact.
            await self._command(f"modify-device|{self._video_alias}|QTY=3+FPS=20+SIZE=0+SAVE=0|")

    async def start(self, device_alias: str, access_token: str) -> dict[str, Any]:
        try:
            await self.connect(device_alias, access_token)
            await self._start_video()
            if self.listen:
                # Video does not wait for audio, nor end without it.
                self._audio_task = asyncio.create_task(self._listen_audio())
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

    # Audio is a media connection of its own on the same signaling (AudioConnectionManager).

    async def _listen_audio(self) -> None:
        """Open the camera's audio connection for a live view."""
        link = AudioLink(self, self._audio)
        try:
            async with self._audio_lock:
                await link.open()
        except asyncio.CancelledError:
            await link.close()
            raise
        except (MyQError, OSError, TimeoutError) as error:
            self.audio_error = str(error) or "The camera's audio connection failed"
            await link.close()
            return
        self._audio_link = link

    async def talk_link(self) -> tuple["AudioLink", bool]:
        """The audio connection to talk over, and whether the caller must close it.

        A live view's connection is shared, as the app talks over its own; without one,
        a connection is opened for the talk.
        """
        if (task := self._audio_task) is not None and not task.done():
            await asyncio.wait({task})
        link = self._audio_link
        if link is not None and link.is_open:
            return link, False
        link = AudioLink(self)
        async with self._audio_lock:
            await link.open()
        return link, True

    @property
    def audio_channel(self) -> tuple[str, int] | None:
        return self._audio_channel

    @property
    def media_sources(self) -> set[str]:
        """The aliases a TLS channel's messages may name as their source."""
        return {self._device_alias, self._video_alias}

    @property
    def camera_address(self) -> str:
        return self._camera_ip

    @property
    def account_alias(self) -> str:
        return self._video_alias

    @property
    def channel_identity(self) -> tuple[bytes, int]:
        """The login's sender and recipient, which join a TLS media channel."""
        return self._sender, self._login_recipient

    async def request_audio(self, session_id: str, mode: int) -> tuple[str, int] | None:
        """require-audio-send; an RTP mode's acceptance names the relay."""
        self._check_open()
        offer = self._audio_offer = asyncio.get_running_loop().create_future()
        try:
            self._audio_mode = mode
            await self._command(f"require-audio-send|{self._video_alias}|{session_id}|{mode}|")
            async with asyncio.timeout(OFFER_TIMEOUT):
                return await offer
        finally:
            self._audio_offer = None
            if offer.done() and not offer.cancelled():
                offer.exception()

    async def audio_command(self, verb: str, session_id: str, mode: int) -> None:
        """start-audio-receive or stop-audio-receive for one talkback session."""
        if verb not in ("start", "stop"):
            raise ProtocolError("Unsupported talkback command")
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            await self._command(f"{verb}-audio-receive|{self._video_alias}|{session_id}|{mode}|")

    async def close(self) -> None:
        """Finish media and TLS teardown even if the requesting viewer is cancelled."""
        if self._legacy_cleanup is None:
            self._legacy_cleanup = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._legacy_cleanup)

    async def _shutdown(self) -> None:
        try:
            task, self._audio_task = self._audio_task, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            link, self._audio_link = self._audio_link, None
            try:
                if link is not None:
                    await link.close()
            finally:
                if self._media is not None:
                    await self._media.close()
        finally:
            self._inventory = ""
            await super().close()


class AudioLink:
    """The camera's audio connection on a signaling session (AudioConnectionManager).

    The camera's microphone arrives on it (``on_audio``), and talkback is sent over it.
    Modes are tried in video's order: UDP to the camera (9, or 8 without a key), then
    the TLS audio channel (0) when the login names one.
    """

    def __init__(self, session: LegacyTendSession, on_audio: AudioCallback | None = None) -> None:
        self._session = session
        self._on_audio = on_audio
        self.session_id = f"{session.account_alias}:{int(time.time() * 1000)}"
        self.mode: int | None = None
        self.frames = 0  # Audio frames the camera sent.
        self.codec: int | None = None  # The codec of the first of them.
        self._heard = asyncio.Event()
        self._rtp: TendRtp | None = None
        self._channel: TendChannel | None = None
        self._stopped = False

    @property
    def is_open(self) -> bool:
        media = self._rtp or self._channel
        return media is not None and not media.ended

    def _modes(self) -> list[int]:
        modes = [9 if self._session._key else 8]
        if self._session.audio_channel is not None:
            modes.append(CHANNEL_MODE)
        return modes

    def _frame(self, codec: int, data: bytes, sequence: int | None) -> None:
        self.frames += 1
        if self.codec is None:
            self.codec = codec
            self._heard.set()
        if self._on_audio is not None:
            self._on_audio(codec, data, sequence)

    async def heard(self, seconds: float) -> int | None:
        """The codec the camera sends in, waiting up to ``seconds`` for its first frame."""
        try:
            async with asyncio.timeout(seconds):
                await self._heard.wait()
        except TimeoutError:
            pass
        return self.codec

    async def open(self) -> None:
        """Ask the camera for its audio connection, trying each mode in turn."""
        session = self._session
        failure: MyQError | None = None
        for mode in self._modes():
            try:
                relay = await session.request_audio(self.session_id, mode)
            except TimeoutError:
                failure = CannotConnect("The camera did not answer the audio request")
                await self._stop_request(mode)
                continue
            self.mode = mode
            try:
                if mode == CHANNEL_MODE:
                    # As for video, the camera is asked to start before the channel joins.
                    await session.audio_command("start", self.session_id, mode)
                    sender, recipient = session.channel_identity
                    channel = self._channel = TendChannel(lambda record: None, on_audio=self._frame)
                    assert session.audio_channel is not None
                    await channel.start(
                        session.audio_channel,
                        sender,
                        recipient,
                        session.media_sources,
                        join=JOIN_AUDIO,
                    )
                else:
                    if relay is None:
                        raise ProtocolError("Tend did not provide an audio relay")
                    rtp = self._rtp = TendRtp(
                        lambda record: None, kind=b"A", watch_silence=False, on_audio=self._frame
                    )
                    await rtp.start(
                        relay, session.account_alias, session.camera_address, session._key
                    )
                    await session.audio_command("start", self.session_id, mode)
                return
            except (CannotConnect, TimeoutError) as error:
                failure = (
                    error
                    if isinstance(error, CannotConnect)
                    else CannotConnect("The camera's audio connection timed out")
                )
                await self.close()
                self.mode, self._stopped = None, False
                continue
        raise failure or CannotConnect("The camera's audio connection could not be opened")

    async def send(self, message: bytes) -> None:
        """One talkback message to the camera."""
        if self._rtp is not None:
            self._rtp.send(message)
        elif self._channel is not None:
            await self._channel.write(message)
        else:
            raise CannotConnect("The camera's audio connection is not open")

    async def _stop_request(self, mode: int) -> None:
        try:
            await self._session.audio_command("stop", self.session_id, mode)
        except (OSError, TimeoutError, MyQError):
            pass

    async def close(self) -> None:
        """stop-audio-receive and close the connection; safe to call more than once."""
        rtp, self._rtp = self._rtp, None
        channel, self._channel = self._channel, None
        try:
            if self.mode is not None and not self._stopped:
                self._stopped = True
                await self._stop_request(self.mode)
        finally:
            if rtp is not None:
                await rtp.close()
            if channel is not None:
                await channel.close()

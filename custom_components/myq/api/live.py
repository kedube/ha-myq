"""Receive Tend/KVS video and relay it to Home Assistant WebRTC viewers.

One native session per camera (Tend, KVS signaling and the camera-facing peer) is
shared by up to MAX_VIEWERS browser-facing peers through an aiortc MediaRelay. The
camera-facing peer uses the ephemeral ICE credentials from the native handshake;
browser-facing peers never receive the MyQ token or signed URL. Microphone uplink is
negotiated only with permission, from the first viewer, and stays muted until enabled.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from fractions import Fraction
from typing import Any, cast

import aiohttp
import av
from aiortc import (
    MediaStreamTrack,
    RTCBundlePolicy,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import SessionDescription, candidate_from_sdp, candidate_to_sdp

from .errors import CannotConnect, CommandRejected, MyQError, ProtocolError
from .kvs import KvsConfig, KvsSession
from .live_controls import LiveControls, MutedMicrophone
from .tend import TendSession

START_TIMEOUT = 45
MAX_VIEW_SECONDS = 600
MAX_SDP = 65536
MAX_CANDIDATES = 128
MAX_VIEWERS = 4  # Local CPU bound: each viewer is encoded separately.
PRIMARY = "primary"
FRAME_TIMEOUT = 2


def _offer_kinds(offer_sdp: str) -> list[str]:
    if not isinstance(offer_sdp, str) or not 1 <= len(offer_sdp) <= MAX_SDP:
        raise ProtocolError("Invalid browser video offer")
    offer = SessionDescription.parse(offer_sdp)
    kinds = [m.kind for m in offer.media]
    if (
        kinds.count("video") != 1
        or kinds.count("audio") > 1
        or any(kind not in {"video", "audio"} for kind in kinds)
        or not any(
            m.kind == "video" and m.direction in {None, "recvonly", "sendrecv"} for m in offer.media
        )
    ):
        raise ProtocolError("The browser must offer to receive camera video")
    return kinds


def encode_jpeg(frame: av.VideoFrame) -> bytes:
    """Encode one decoded live frame as a JPEG with FFmpeg's MJPEG encoder."""
    codec = cast(av.VideoCodecContext, av.CodecContext.create("mjpeg", "w"))
    codec.width, codec.height = frame.width, frame.height
    codec.pix_fmt = "yuvj420p"
    codec.time_base = Fraction(1, 30)
    packets = codec.encode(frame.reformat(format="yuvj420p")) + codec.encode(None)
    data = b"".join(bytes(packet) for packet in packets)
    if not data.startswith(b"\xff\xd8\xff"):
        raise ProtocolError("Could not encode the live frame")
    return data


class _Viewer:
    """One browser-facing peer fed from the shared relay."""

    def __init__(self, ice: list[RTCIceServer]) -> None:
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        self.kinds: list[str] = []
        self.candidates: list[dict[str, Any]] = []
        self.microphone: MutedMicrophone | None = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self.microphone is not None:
            self.microphone.stop()
        self.candidates.clear()
        await self.peer.close()


class LiveSession:
    """One native camera session shared by up to MAX_VIEWERS browser peers."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        on_error: Callable[[MyQError], Awaitable[None]],
        *,
        browser_ice_servers: list[RTCIceServer] | None = None,
        allow_talkback: bool = False,
        on_status: Callable[[], None] | None = None,
    ) -> None:
        self._http = session
        self._on_error = on_error
        self._browser_ice = browser_ice_servers or []
        self._tend: TendSession | None = None
        self._kvs: KvsSession | None = None
        self._camera: RTCPeerConnection | None = None
        self._viewers: dict[str, _Viewer] = {}
        self._relay = MediaRelay()
        self._sources: dict[str, MediaStreamTrack] = {}
        self._answer: asyncio.Future[None] | None = None
        self._remote_candidates: list[dict[str, Any]] = []
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._failed = False
        self._started = False
        self._connected = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._allow_talkback = allow_talkback is True
        self._on_status = on_status
        self._microphone: MutedMicrophone | None = None
        self._controls: LiveControls | None = None
        self._control_details: dict = {}
        self._can_control = False
        self._ready = False

    @property
    def _browser(self) -> RTCPeerConnection | None:
        """The first viewer's peer (kept for diagnostics and tests)."""
        viewer = self._viewers.get(PRIMARY) or next(iter(self._viewers.values()), None)
        return viewer.peer if viewer is not None else None

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def viewers(self) -> int:
        return sum(not viewer.closed for viewer in self._viewers.values())

    def configure_controls(self, details: dict, *, can_control: bool) -> None:
        """Apply refreshed capability, availability and account permission gates."""
        self._control_details, self._can_control = details, can_control is True
        if self._controls is not None:
            self._controls.configure(details, can_control=can_control)
        if self._microphone is not None and self._microphone.enabled:
            try:
                self._require_controls().require("talkback")
            except MyQError:
                self._microphone.enable(False)

    def _require_controls(self) -> LiveControls:
        if not self._ready or self._closed or self._controls is None:
            raise CommandRejected("Open a live view before controlling this camera")
        return self._controls

    async def ptz(self, action: str) -> None:
        await self._require_controls().ptz(action)

    async def siren(self, enabled: bool, *, duration: int = 10) -> None:
        await self._require_controls().siren(enabled, duration=duration)

    async def send_command(self, command: str, actions: tuple[tuple[str, str], ...]) -> None:
        """A one-off native command (preset move) over this view's Tend session."""
        if not self.ready or self._tend is None:
            raise CommandRejected("Open a live view before controlling this camera")
        await self._tend.send_to(self._tend._device_alias, command, actions)

    def set_talkback(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise CommandRejected("Invalid microphone action")
        if not enabled:
            if self._microphone is not None:
                self._microphone.enable(False)
            return
        self._require_controls().require("talkback")
        if self._microphone is None or self._microphone.readyState != "live":
            raise CommandRejected("The viewer must offer a microphone audio track")
        if self._camera is None:
            raise CommandRejected("Open a live view before controlling this camera")
        audio = next((t for t in self._camera.getTransceivers() if t.kind == "audio"), None)
        if audio is None or audio.currentDirection not in {"sendonly", "sendrecv"}:
            raise CommandRejected("Camera did not accept microphone audio")
        self._microphone.enable(True)

    def _spawn(self, operation: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(
        self, alias: str, access_token: str, offer_sdp: str, viewer: str = PRIMARY
    ) -> str:
        """Start the native session and return the first viewer's browser answer."""
        if self._started or self._closed:
            raise ProtocolError("The camera session cannot be reused")
        self._started = True
        try:
            async with asyncio.timeout(START_TIMEOUT):
                return await self._start(alias, access_token, offer_sdp, viewer)
        except asyncio.CancelledError:
            await self.close()
            raise
        except MyQError:
            await self.close()
            raise
        except Exception:
            # SDP, TURN credentials, and signed URLs can occur in library errors.
            await self.close()
            raise CannotConnect("Unable to establish the MyQ live video session") from None

    async def join(self, offer_sdp: str, viewer: str) -> str:
        """Add another browser viewer to the running native session."""
        if not self.ready:
            raise CommandRejected("The live camera session is not ready")
        if viewer in self._viewers:
            raise ProtocolError("This viewer already joined")
        if self.viewers >= MAX_VIEWERS:
            raise CommandRejected("Too many viewers are watching this camera")
        entry: _Viewer | None = None
        try:
            async with asyncio.timeout(START_TIMEOUT):
                entry = await self._prepare_viewer(viewer, offer_sdp, primary=False)
                self._attach(entry)
                if self.ready is not True:
                    raise CannotConnect("The live camera session ended")
                await entry.peer.setLocalDescription(await entry.peer.createAnswer())
                return entry.peer.localDescription.sdp
        except Exception as err:
            await self.leave(viewer)
            if isinstance(err, MyQError):
                raise
            raise CannotConnect("Unable to add the live video viewer") from None

    async def _prepare_viewer(self, viewer: str, offer_sdp: str, *, primary: bool) -> _Viewer:
        kinds = _offer_kinds(offer_sdp)
        entry = self._viewers.setdefault(viewer, _Viewer(self._browser_ice))
        entry.kinds = kinds
        pending = list(entry.candidates)

        @entry.peer.on("track")
        def browser_track(track) -> None:
            # Only the first viewer's microphone can feed the camera's audio sender.
            if track.kind == "audio" and primary and self._allow_talkback:
                entry.microphone = MutedMicrophone(track)
                self._microphone = entry.microphone
            else:
                track.stop()

        @entry.peer.on("connectionstatechange")
        async def on_state() -> None:
            if entry.peer.connectionState in {"failed", "closed"} and not entry.closed:
                self._spawn(self.leave(viewer, error=CannotConnect("The viewer connection ended")))

        await entry.peer.setRemoteDescription(RTCSessionDescription(offer_sdp, "offer"))
        for candidate in pending:
            await self._add_candidate(entry.peer, candidate)
        entry.candidates.clear()
        # Permission permits negotiation only. The outgoing track starts muted.
        for transceiver in entry.peer.getTransceivers():
            transceiver.direction = (
                "recvonly" if transceiver.kind == "audio" and entry.microphone else "inactive"
            )
        return entry

    def _attach(self, entry: _Viewer) -> None:
        """Feed the shared camera tracks to one viewer through the relay."""
        for kind, source in self._sources.items():
            if kind not in entry.kinds:
                continue
            # Video takes the newest frame; audio keeps every frame.
            track = self._relay.subscribe(source, buffered=kind == "audio")
            entry.peer.addTrack(track)
            for transceiver in entry.peer.getTransceivers():
                if transceiver.sender.track is track:
                    transceiver.direction = (
                        "sendrecv" if kind == "audio" and entry.microphone else "sendonly"
                    )

    async def _start(self, alias: str, token: str, offer_sdp: str, viewer: str) -> str:
        entry = await self._prepare_viewer(viewer, offer_sdp, primary=True)
        kinds = entry.kinds

        self._tend = TendSession()
        self._tend.on_status = self._on_status
        options = await self._tend.start(alias, token)
        self._controls = LiveControls(self._tend)
        self._controls.configure(self._control_details, can_control=self._can_control)
        self._spawn(self._watch_tend())
        config = KvsConfig.from_server_options(options)
        # PeerConnectionModel: MAXBUNDLE with rtcp-mux, one transport for both lines.
        self._camera = RTCPeerConnection(
            RTCConfiguration(
                iceServers=[
                    RTCIceServer(list(ice.urls), username=ice.username, credential=ice.password)
                    for ice in config.ice_servers
                ],
                bundlePolicy=RTCBundlePolicy.MAX_BUNDLE,
            )
        )

        @self._camera.on("track")
        def on_track(track) -> None:
            if self._closed or track.kind not in kinds or track.kind in self._sources:
                track.stop()
                return
            self._sources[track.kind] = track

        @self._camera.on("connectionstatechange")
        async def on_camera_state() -> None:
            assert self._camera is not None
            if self._camera.connectionState == "connected":
                self._connected = True
            if self._camera.connectionState in {"failed", "closed"} and not self._closed:
                self._spawn(self._fail(CannotConnect("The MyQ video connection ended")))

        # The app selects H.264 and G.711 for this camera route, with recv-only
        # transceivers; native PeerConnectionModel adds a muted local audio track.
        for kind in ("video", "audio"):
            if kind not in kinds:
                continue
            transceiver = self._camera.addTransceiver(
                self._microphone if kind == "audio" and self._microphone else kind,
                direction="sendrecv" if kind == "audio" and self._microphone else "recvonly",
            )
            permitted = (
                {"video/h264", "video/rtx"} if kind == "video" else {"audio/pcmu", "audio/pcma"}
            )
            transceiver.setCodecPreferences(
                [
                    c
                    for c in RTCRtpSender.getCapabilities(kind).codecs
                    if c.mimeType.lower() in permitted
                ]
            )
        self._answer = asyncio.get_running_loop().create_future()
        self._kvs = KvsSession(self._http, config, self._message, self._signaling_error)
        await self._kvs.open()
        await self._camera.setLocalDescription(await self._camera.createOffer())
        local = SessionDescription.parse(self._camera.localDescription.sdp)
        candidates: list[dict[str, Any]] = []
        for index, media in enumerate(local.media):
            candidates.extend(
                {
                    "candidate": "candidate:" + candidate_to_sdp(candidate),
                    "sdpMid": media.rtp.muxId,
                    "sdpMLineIndex": index,
                }
                for candidate in media.ice_candidates
            )
            media.ice_candidates = []
            media.ice_candidates_complete = False
        # Match the app's SDP offer followed by separate ICE_CANDIDATE messages.
        await self._kvs.send_offer(str(local))
        for candidate in candidates:
            await self._kvs.send_candidate(candidate)
        await self._answer
        if self._closed or "video" not in self._sources:
            raise ProtocolError("The MyQ session did not negotiate a video track")
        self._attach(entry)
        await entry.peer.setLocalDescription(await entry.peer.createAnswer())
        answer = entry.peer.localDescription.sdp
        self._ready = True
        self._timer = asyncio.get_running_loop().call_later(
            MAX_VIEW_SECONDS,
            lambda: self._spawn(self._fail(CannotConnect("Live view ended; reopen the camera"))),
        )
        return answer

    async def snapshot(self) -> bytes | None:
        """A JPEG of the newest live frame (the live player's snapshot), if one arrives."""
        source = self._sources.get("video")
        if not self.ready or source is None:
            return None
        track = self._relay.subscribe(source, buffered=False)
        try:
            async with asyncio.timeout(FRAME_TIMEOUT):
                frame = await track.recv()
        except Exception:
            return None
        finally:
            track.stop()
        if not isinstance(frame, av.VideoFrame):
            return None
        try:
            return await asyncio.get_running_loop().run_in_executor(None, encode_jpeg, frame)
        except Exception:
            return None

    async def _message(self, message: dict[str, Any]) -> None:
        if self._closed or self._camera is None:
            return
        if message["type"] == "answer":
            if self._camera.remoteDescription is not None:
                if self._connected:
                    return  # The app ignores late signaling once media flows.
                raise ProtocolError("Unexpected repeated camera video answer")
            await self._camera.setRemoteDescription(RTCSessionDescription(message["sdp"], "answer"))
            for candidate in self._remote_candidates:
                await self._add_candidate(self._camera, candidate)
            self._remote_candidates.clear()
            if self._answer is not None and not self._answer.done():
                self._answer.set_result(None)
        elif message["type"] == "candidate":
            if self._camera.remoteDescription is None:
                if len(self._remote_candidates) >= MAX_CANDIDATES:
                    raise ProtocolError("Too many camera ICE candidates")
                self._remote_candidates.append(message)
            else:
                try:
                    await self._add_candidate(self._camera, message)
                except MyQError:
                    if not self._connected:
                        raise

    async def candidate(self, candidate: dict[str, Any], viewer: str = PRIMARY) -> None:
        """Accept trickled ICE from a browser viewer, including during setup."""
        if self._closed:
            return
        entry = self._viewers.get(viewer)
        if entry is None or entry.peer.remoteDescription is None:
            if entry is None:
                entry = self._viewers.setdefault(viewer, _Viewer(self._browser_ice))
            if len(entry.candidates) >= MAX_CANDIDATES:
                raise ProtocolError("Too many browser ICE candidates")
            entry.candidates.append(candidate)
            return
        try:
            await self._add_candidate(entry.peer, candidate)
        except Exception:
            raise ProtocolError("Invalid browser ICE candidate") from None

    @staticmethod
    async def _add_candidate(peer: RTCPeerConnection, value: dict[str, Any]) -> None:
        raw = value.get("candidate")
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ProtocolError("Invalid ICE candidate")
        if not raw:
            await peer.addIceCandidate(None)
            return
        mid, index = value.get("sdpMid"), value.get("sdpMLineIndex")
        if (
            (mid is not None and (not isinstance(mid, str) or len(mid) > 64))
            or (index is not None and (type(index) is not int or not 0 <= index <= 8))
            or (mid is None and index is None)
        ):
            raise ProtocolError("Invalid ICE candidate media identifier")
        parsed = candidate_from_sdp(raw.removeprefix("candidate:"))
        parsed.sdpMid, parsed.sdpMLineIndex = mid, index
        await peer.addIceCandidate(parsed)

    async def leave(self, viewer: str, *, error: MyQError | None = None) -> None:
        """Remove one viewer; the native session ends with its last viewer."""
        entry = self._viewers.pop(viewer, None)
        if entry is not None:
            if entry.microphone is not None and entry.microphone is self._microphone:
                self._microphone.enable(False)
            await entry.close()
        if not self.viewers and not self._closed:
            if error is not None and self._started:
                await self._fail(error)
            elif self._started:
                await self.close()

    async def _fail(self, error: MyQError) -> None:
        if self._closed or self._failed:
            return
        self._failed = True
        if self._answer is not None and not self._answer.done():
            self._answer.set_exception(error)
        await self.close()
        await self._on_error(error)

    async def _signaling_error(self, error: MyQError) -> None:
        # PeerConnectionModel ignores signaling failures once the peers are connected:
        # the KVS channel is only needed to negotiate, not to carry media.
        if self._connected and self._ready:
            return
        # Never await the signaling reader's own shutdown from its callback.
        self._spawn(self._fail(error))

    async def _watch_tend(self) -> None:
        if (tend := self._tend) is None:
            return
        try:
            await tend.wait_closed()
            if not self._closed:
                await self._fail(CannotConnect("The MyQ camera session ended"))
        except MyQError as err:
            await self._fail(err)

    async def close(self) -> None:
        """Retain cleanup even when the caller is cancelled."""
        if self._cleanup_task is None:
            self._closed = True
            self._ready = False
            self._cleanup_task = asyncio.create_task(self._close_impl(asyncio.current_task()))
        await asyncio.shield(self._cleanup_task)

    async def _close_impl(self, initiator: asyncio.Task | None) -> None:
        if self._microphone is not None:
            self._microphone.stop()
        if self._timer is not None:
            self._timer.cancel()
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        elif self._answer is not None and not self._answer.cancelled():
            self._answer.exception()
        # A watcher may initiate shutdown and still need to report its failure.
        # Cancelling/joining it here would interrupt the caller awaiting this task.
        tasks = [task for task in self._tasks if task not in {initiator, asyncio.current_task()}]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._controls is not None:
            await self._controls.close()
        # Viewers stay listed, closed, so callers can inspect the ended session.
        viewers = list(self._viewers.values())
        # Closing every peer stops their DTLS/SRTP/ICE tasks, receivers and relay feeds.
        resources = [self._kvs, self._camera, self._tend]
        await asyncio.gather(
            *(viewer.close() for viewer in viewers),
            *(resource.close() for resource in resources if resource is not None),
            return_exceptions=True,
        )
        self._remote_candidates.clear()

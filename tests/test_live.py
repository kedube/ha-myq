"""Actual WebRTC media on loopback, without a MyQ account or physical camera."""

import asyncio
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import av
import pytest
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)

from custom_components.myq.api import live
from custom_components.myq.api.errors import CannotConnect, CommandRejected, ProtocolError
from custom_components.myq.api.kvs import KvsConfig


async def test_cancelled_close_preserves_cleanup_and_other_waiters():
    entered, release = asyncio.Event(), asyncio.Event()

    async def controls_close():
        entered.set()
        await release.wait()

    stream = live.LiveSession(None, AsyncMock())
    stream._controls = SimpleNamespace(close=AsyncMock(side_effect=controls_close))
    resources = [SimpleNamespace(close=AsyncMock()) for _ in range(4)]
    stream._kvs, stream._camera, stream._tend = resources[:3]
    stream._viewers = {"primary": resources[3]}
    first = asyncio.create_task(stream.close())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(stream.close())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        release.set()
        await asyncio.wait_for(second, 1)
    finally:
        release.set()
        await stream.close()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    stream._controls.close.assert_awaited_once()
    for resource in resources:
        resource.close.assert_awaited_once()


@pytest.fixture
def video_service(monkeypatch):
    """Keep the native signaling boundaries fake, and the media peers real."""
    instances = []
    # HA's test fixture supplies fake interface addresses. Use real loopback
    # candidates and no public STUN/TURN server for these media tests.
    monkeypatch.setattr("aioice.ice.get_host_addresses", lambda **kwargs: ["127.0.0.1"])

    class Tend:
        def __init__(self):
            self.ended = asyncio.Event()
            self.start = AsyncMock(return_value={"kvs": {}})
            self.close = AsyncMock(side_effect=self.ended.set)
            instances.append(self)

        async def wait_closed(self):
            await self.ended.wait()

    class Kvs:
        def __init__(self, http, config, callback, on_error):
            self.remote = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.remote.addTrack(VideoStreamTrack())
            self.callback, self.on_error = callback, on_error
            self.open = AsyncMock()
            self.close = AsyncMock(side_effect=self.remote.close)
            self.audio_frames = asyncio.Queue()
            self.audio_tasks = []

            @self.remote.on("track")
            def received(track):
                if track.kind == "audio":

                    async def read_audio():
                        from aiortc.mediastreams import MediaStreamError

                        try:
                            while True:
                                frame = await track.recv()
                                self.audio_frames.put_nowait(abs(frame.to_ndarray()).max())
                        except MediaStreamError:
                            pass

                    task = asyncio.create_task(read_audio())
                    self.audio_tasks.append(task)

            instances.append(self)

        async def send_offer(self, sdp):
            await self.remote.setRemoteDescription(RTCSessionDescription(sdp, "offer"))
            await self.remote.setLocalDescription(await self.remote.createAnswer())
            await self.callback({"type": "answer", "sdp": self.remote.localDescription.sdp})

        async def send_candidate(self, candidate):
            await live.LiveSession._add_candidate(self.remote, candidate)

    monkeypatch.setattr(live, "TendSession", Tend)
    monkeypatch.setattr(live, "KvsSession", Kvs)
    monkeypatch.setattr(
        live.KvsConfig,
        "from_server_options",
        lambda options: KvsConfig(
            "wss://test.kinesisvideo.us-east-1.amazonaws.com", "us-east-1", ()
        ),
    )
    return instances


async def make_browser():
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.addTransceiver("video", direction="recvonly")
    await browser.setLocalDescription(await browser.createOffer())
    return browser


class ToneMicrophone(AudioStreamTrack):
    async def recv(self):
        frame = await super().recv()
        # A changing waveform survives Opus' DC rejection on the browser leg.
        frame.planes[0].update((b"\x00\x20" * 8 + b"\x00\xe0" * 8) * (frame.samples // 16))
        return frame


@pytest.mark.usefixtures("socket_enabled")
async def test_real_microphone_uplink_is_muted_until_explicit_start(video_service):
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.addTransceiver("video", direction="recvonly")
    browser.addTrack(ToneMicrophone())
    await browser.setLocalDescription(await browser.createOffer())
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, AsyncMock(), allow_talkback=True)
        stream.configure_controls(
            {
                "cap": {"spk": {"talkback": True}},
                "settings": {"cam": {"enabled": True}},
                "status": {"online": True},
            },
            can_control=True,
        )
        try:
            async with asyncio.timeout(15):
                answer = await stream.start("alias", "token", browser.localDescription.sdp)
                await browser.setRemoteDescription(RTCSessionDescription(answer, "answer"))
                _, kvs = video_service
                assert await kvs.audio_frames.get() < 100
                stream.set_talkback(True)
                for _ in range(50):
                    if await kvs.audio_frames.get() > 1000:
                        break
                else:
                    pytest.fail("Enabled browser microphone did not reach the camera peer")
                stream.set_talkback(False)
                for _ in range(50):
                    if await kvs.audio_frames.get() < 100:
                        break
                else:
                    pytest.fail("Disabled microphone continued forwarding sound")
        finally:
            await stream.close()
            await browser.close()
            if len(video_service) > 1:
                await asyncio.gather(*video_service[1].audio_tasks, return_exceptions=True)


@pytest.mark.usefixtures("socket_enabled")
async def test_real_h264_media_reaches_viewer_and_all_peers_close(video_service):
    browser = await make_browser()
    frames = asyncio.Queue()
    receiver_tasks = []

    @browser.on("track")
    def track_added(track):
        async def receive():
            frames.put_nowait(await track.recv())

        receiver_tasks.append(asyncio.create_task(receive()))

    errors = AsyncMock()
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, errors)
        try:
            async with asyncio.timeout(15):
                answer = await stream.start(
                    "camera-alias", "private-token", browser.localDescription.sdp
                )
                await browser.setRemoteDescription(RTCSessionDescription(answer, "answer"))
                frame = await frames.get()
            assert (frame.width, frame.height) == (640, 480)
            tend, kvs = video_service
            tend.start.assert_awaited_once_with("camera-alias", "private-token")
            assert "H264/90000" in kvs.remote.remoteDescription.sdp
            assert "a=recvonly" in kvs.remote.remoteDescription.sdp
            assert "a=sendonly" in answer
            errors.assert_not_awaited()
            await stream.close()
            await stream.close()
            assert stream._camera.connectionState == "closed"
            assert stream._browser.connectionState == "closed"
            assert kvs.remote.connectionState == "closed"
            tend.close.assert_awaited_once()
            kvs.close.assert_awaited_once()
            assert not http.closed
        finally:
            await stream.close()
            await browser.close()
            for task in receiver_tasks:
                task.cancel()
            await asyncio.gather(*receiver_tasks, return_exceptions=True)


@pytest.mark.usefixtures("socket_enabled")
async def test_signaling_loss_closes_media_and_reports_error(video_service):
    browser = await make_browser()
    error_received = asyncio.Event()
    errors = AsyncMock(side_effect=lambda error: error_received.set())
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, errors)
        try:
            await stream.start("camera-alias", "private-token", browser.localDescription.sdp)
            tend, kvs = video_service
            tend.ended.set()
            async with asyncio.timeout(5):
                await error_received.wait()
            assert stream._camera.connectionState == "closed"
            assert kvs.remote.connectionState == "closed"
            errors.assert_awaited_once()
        finally:
            await stream.close()
            await browser.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_cancellation_during_native_handshake_releases_session(video_service, monkeypatch):
    browser = await make_browser()
    entered = asyncio.Event()

    class WaitingTend:
        async def start(self, alias, token):
            entered.set()
            await asyncio.Event().wait()

        close = AsyncMock()

    monkeypatch.setattr(live, "TendSession", WaitingTend)
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, AsyncMock())
        task = asyncio.create_task(stream.start("alias", "token", browser.localDescription.sdp))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        WaitingTend.close.assert_awaited_once()
        assert stream._browser.connectionState == "closed"
        assert stream._closed
    await browser.close()


async def test_invalid_offer_does_not_contact_tend(video_service):
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, AsyncMock())
        with pytest.raises(ProtocolError, match="offer"):
            await stream.start("alias", "token", "v=0\r\n")
        assert not video_service
        assert stream._closed


@pytest.mark.usefixtures("socket_enabled")
async def test_library_error_is_sanitized_and_closes_resources(video_service, monkeypatch):
    browser = await make_browser()

    class FailedTend:
        async def start(self, alias, token):
            raise ValueError("private-token signed-channel-url")

        close = AsyncMock()

    monkeypatch.setattr(live, "TendSession", FailedTend)
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, AsyncMock())
        with pytest.raises(CannotConnect) as error:
            await stream.start("alias", "private-token", browser.localDescription.sdp)
        assert "private-token" not in str(error.value)
        assert "signed-channel-url" not in str(error.value)
        assert stream._browser.connectionState == "closed"
        FailedTend.close.assert_awaited_once()
    await browser.close()


@pytest.fixture
def legacy_service(monkeypatch):
    """Supply locally encoded Annex-B records at the native transport boundary."""
    monkeypatch.setattr("aioice.ice.get_host_addresses", lambda **kwargs: ["127.0.0.1"])
    codec = av.CodecContext.create("libx264", "w")
    codec.width, codec.height = 64, 48
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, 15)
    codec.framerate = Fraction(15)
    codec.options = {"preset": "ultrafast", "tune": "zerolatency", "g": "1"}
    frame = av.VideoFrame(64, 48, "yuv420p")
    for plane in frame.planes:
        plane.update(bytes([100]) * plane.buffer_size)
    frame.pts = 0
    record = b"".join(bytes(packet) for packet in codec.encode(frame))
    assert record.startswith(b"\x00\x00\x00\x01")
    instances = []

    class Legacy:
        def __init__(self):
            self.ended = asyncio.Event()
            self.start = AsyncMock(return_value={})
            self.close = AsyncMock(side_effect=self.ended.set)
            self.error = None
            self.records = 0
            self.twelve_records = asyncio.Event()
            self.allow_frames = asyncio.Event()
            self.allow_frames.set()
            instances.append(self)

        async def recv(self):
            await self.allow_frames.wait()
            await asyncio.sleep(1 / 30)
            if self.error:
                raise self.error
            self.records += 1
            if self.records == 12:
                self.twelve_records.set()
            return record

        async def wait_closed(self):
            await self.ended.wait()

    monkeypatch.setattr(live, "LegacyTendSession", Legacy)
    return instances


@pytest.mark.usefixtures("socket_enabled")
async def test_legacy_h264_is_shared_with_viewers_and_snapshots(legacy_service):
    browsers = [await make_browser(), await make_browser()]
    received = [asyncio.Queue(), asyncio.Queue()]
    readers = []

    def watch(browser, queue):
        @browser.on("track")
        def track_added(track):
            async def receive():
                queue.put_nowait(await track.recv())

            readers.append(asyncio.create_task(receive()))

    for browser, queue in zip(browsers, received, strict=True):
        watch(browser, queue)
    errors = AsyncMock()
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, errors, legacy=True, allow_talkback=True)
        try:
            async with asyncio.timeout(15):
                answer = await stream.start(
                    "camera-alias", "private-token", browsers[0].localDescription.sdp
                )
                await browsers[0].setRemoteDescription(RTCSessionDescription(answer, "answer"))
                first = await received[0].get()
                answer2 = await stream.join(browsers[1].localDescription.sdp, "second")
                await browsers[1].setRemoteDescription(RTCSessionDescription(answer2, "answer"))
                second = await received[1].get()
                jpeg = await stream.snapshot()
            assert (first.width, first.height) == (second.width, second.height) == (64, 48)
            assert jpeg.startswith(b"\xff\xd8\xff")
            assert stream.ready and stream.viewers == 2
            assert stream._camera is None and stream._kvs is None
            assert "private-token" not in answer + answer2
            with pytest.raises(CommandRejected, match="Talkback is unavailable"):
                stream.set_talkback(True)
            assert len(legacy_service) == 1
            tend = legacy_service[0]
            tend.start.assert_awaited_once_with("camera-alias", "private-token")
            await stream.leave("primary")
            assert stream.ready and stream.viewers == 1
            tend.close.assert_not_awaited()
            await stream.leave("second")
            assert not stream.ready
            assert stream._legacy_video.readyState == "ended"
            assert stream._legacy_video._task.done()
            tend.close.assert_awaited_once()
            errors.assert_not_awaited()
        finally:
            await stream.close()
            await asyncio.gather(*(browser.close() for browser in browsers))
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)


@pytest.mark.usefixtures("socket_enabled")
async def test_legacy_receive_failure_closes_decoder_and_viewers_without_deadlock(legacy_service):
    browser = await make_browser()
    ended = asyncio.Event()
    errors = AsyncMock(side_effect=lambda error: ended.set())
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, errors, legacy=True)
        try:
            await stream.start("alias", "token", browser.localDescription.sdp)
            legacy_service[0].error = CannotConnect("The camera stopped sending video")
            async with asyncio.timeout(5):
                await ended.wait()
            assert stream._browser.connectionState == "closed"
            assert stream._legacy_video.readyState == "ended"
            assert stream._legacy_video._task.done()
            legacy_service[0].close.assert_awaited_once()
            errors.assert_awaited_once()
        finally:
            await stream.close()
            await browser.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_cancelling_legacy_start_before_first_frame_cleans_up(legacy_service, monkeypatch):
    receiving = asyncio.Event()

    class WaitingLegacy(live.LegacyTendSession):
        async def recv(self):
            receiving.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(live, "LegacyTendSession", WaitingLegacy)
    browser = await make_browser()
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, AsyncMock(), legacy=True)
        task = asyncio.create_task(stream.start("alias", "token", browser.localDescription.sdp))
        try:
            async with asyncio.timeout(5):
                await receiving.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert stream._browser.connectionState == "closed"
            assert stream._legacy_video._task.done()
            assert stream._legacy_video.readyState == "ended"
            legacy_service[0].close.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await stream.close()
            await browser.close()


async def test_legacy_decoder_bounds_frames_and_preserves_monotonic_timestamps(legacy_service):
    source = live.LegacyTendSession()
    errors = []
    track = live._LegacyVideoTrack(source, errors.append)
    try:
        async with asyncio.timeout(5):
            await track.wait_ready()
            first = await track.recv()
            await source.twelve_records.wait()
            assert track._frames.qsize() == 2
            latest = await track.recv()
        assert latest.pts > first.pts
        assert first.time_base == latest.time_base == Fraction(1, 90000)
        assert not errors
    finally:
        await track.close()
    assert track._task.done()


@pytest.mark.parametrize("record", [b"", "not bytes", b"x" * (live.MAX_H264_RECORD + 1)])
async def test_legacy_decoder_rejects_invalid_records(record):
    errors = []
    source = SimpleNamespace(recv=AsyncMock(return_value=record))
    track = live._LegacyVideoTrack(source, errors.append)
    try:
        with pytest.raises(ProtocolError, match="Invalid camera video record"):
            await track.wait_ready()
        assert len(errors) == 1
    finally:
        await track.close()


async def test_legacy_decoder_times_out_when_no_frames_arrive(monkeypatch):
    monkeypatch.setattr(live, "LEGACY_FRAME_TIMEOUT", 0.02)

    async def no_records():
        await asyncio.Event().wait()

    errors = []
    track = live._LegacyVideoTrack(SimpleNamespace(recv=no_records), errors.append)
    try:
        with pytest.raises(CannotConnect, match="Unable to decode"):
            await track.wait_ready()
        assert len(errors) == 1
    finally:
        await track.close()

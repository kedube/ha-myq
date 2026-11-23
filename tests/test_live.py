"""Actual WebRTC media on loopback, without a MyQ account or physical camera."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)

from custom_components.myq.api import live
from custom_components.myq.api.errors import CannotConnect, ProtocolError
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
        stream.wakeup, stream.camera_asleep = 1, True
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
            assert (tend.wakeup, tend.camera_asleep) == (1, True)
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


@pytest.mark.usefixtures("socket_enabled")
async def test_camera_frames_do_not_pile_up_before_a_viewer_connects(video_service):
    """aiortc queues every decoded frame of a received track until something reads it.

    A browser that never completes its connection used to leave them all queued.
    """
    browser = await make_browser()
    async with aiohttp.ClientSession() as http:
        stream = live.LiveSession(http, AsyncMock())
        try:
            async with asyncio.timeout(15):
                await stream.start("camera-alias", "private-token", browser.localDescription.sdp)
            # The browser never applies the answer, so its peer never connects.
            deadline = asyncio.get_running_loop().time() + 3
            received = 0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
                queued = stream._sources["video"]._queue.qsize()
                assert queued < 10
                received = max(received, queued)
            assert stream.ready
        finally:
            await stream.close()
            await browser.close()
            if len(video_service) > 1:
                await asyncio.gather(*video_service[1].audio_tasks, return_exceptions=True)


async def test_owner_is_told_when_the_session_ends_after_dropping_a_viewer():
    """A dropped viewer may still be listed by the owner, which must hear of the end."""
    errors = AsyncMock()
    stream = live.LiveSession(None, errors)
    stream._started = stream._ready = True
    stream._viewers = {
        name: SimpleNamespace(close=AsyncMock(), closed=False, microphone=None) for name in "ab"
    }
    await stream.leave("a", error=CannotConnect("The viewer connection ended"))
    errors.assert_not_awaited()
    await stream.leave("b")
    assert stream._closed
    errors.assert_awaited_once()
    assert str(errors.await_args.args[0]) == "The live view ended"


async def test_viewer_close_finishes_even_if_the_task_closing_it_is_cancelled():
    viewer = live._Viewer([])
    started, finish = asyncio.Event(), asyncio.Event()
    closes = 0

    async def slow_close():
        nonlocal closes
        closes += 1
        started.set()
        await finish.wait()

    viewer.peer.close = slow_close
    first = asyncio.create_task(viewer.close())
    await started.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    second = asyncio.create_task(viewer.close())
    await asyncio.sleep(0)
    finish.set()
    async with asyncio.timeout(1):
        await second
    assert closes == 1 and viewer.closed

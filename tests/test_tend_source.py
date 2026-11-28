"""The TC camera's loopback H.264 address, read by a real local HTTP client."""

import asyncio
from urllib.parse import urlsplit

import aiohttp
import pytest

from custom_components.myq.api import tend_source
from custom_components.myq.api.errors import CannotConnect, CommandRejected
from custom_components.myq.api.live_audio import SILENCE
from custom_components.myq.api.mpegts import PACKET_SIZE, opus_samples
from custom_components.myq.api.tend_source import TendSource, _Reader, nal_units

from .test_live_audio import opus_packets
from .ts import AUDIO_PID, VIDEO_PID, parse, read_stream, read_until, read_video, video

START = b"\x00\x00\x00\x01"
SPS, PPS = b"\x67sps", b"\x68pps"
# The byte after the NAL header starts the slice: its top bit marks a picture's first slice.
IDR, SLICE = b"\x65\x88keyframe", b"\x41\x9adelta"
IDR_NEXT_SLICE = b"\x65\x21more of the same picture"


def record(*units, code=START):
    return b"".join(code + unit for unit in units)


class FakeSession:
    def __init__(self):
        self.ended = asyncio.get_running_loop().create_future()
        self.closed = False
        self.media = None

    async def wait_closed(self):
        await asyncio.shield(self.ended)

    async def close(self):
        self.closed = True
        if not self.ended.done():
            self.ended.set_result(None)


class Camera:
    """Stands in for the entity: opens fake sessions and records their release."""

    def __init__(self):
        self.sessions = []
        self.released = []
        self.stopped = []  # Why sessions that had started ended on their own.
        self.error = None
        self.gate = None
        self.send = None
        self.audio = None

    async def open(self, on_video, on_audio):
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.send, self.audio = on_video, on_audio
        self.sessions.append(FakeSession())
        return self.sessions[-1]

    async def closed(self, session):
        self.released.append(session)


@pytest.fixture
async def source(socket_enabled):
    camera = Camera()
    served = TendSource(camera.open, camera.closed, camera.stopped.append)
    async with aiohttp.ClientSession() as http:
        yield served, camera, http
    await served.close()


async def read(response, size):
    async with asyncio.timeout(2):
        return await response.content.readexactly(size)


async def until(condition):
    """Poll state the source changes from its own tasks (there is no event to await)."""
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


def test_nal_units_from_either_start_code_without_trailing_zero_bytes():
    assert nal_units(record(SPS, PPS, IDR)) == [SPS, PPS, IDR]
    assert nal_units(record(SPS, IDR, code=b"\x00\x00\x01")) == [SPS, IDR]
    assert nal_units(b"junk" + START + SLICE + b"\x00\x00" + b"\x00\x00\x01" + IDR + b"\x00") == [
        SLICE,
        IDR,
    ]
    assert nal_units(b"") == nal_units(b"no start code") == nal_units(START) == []


async def test_reader_starts_at_a_keyframe_led_by_its_parameter_sets(source):
    served, camera, http = source
    url = await served.url()
    assert url == await served.url()
    parts = urlsplit(url)
    assert parts.hostname == "127.0.0.1" and parts.path.endswith(".ts") and len(parts.path) > 30
    async with http.get(url) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "video/mp2t"
        # Mid-stream records are no use to a new reader. Parameter sets sent on their
        # own are held for the keyframe; every unit leaves with a 4-byte start code.
        camera.send(record(SLICE))
        camera.send(record(SPS, code=b"\x00\x00\x01"))
        camera.send(record(PPS))
        camera.send(b"audio or anything without a start code")
        camera.send(record(IDR, code=b"\x00\x00\x01"))
        camera.send(record(IDR_NEXT_SLICE))
        camera.send(record(SLICE))
        expected = (
            record(SPS, PPS, IDR) + record(IDR_NEXT_SLICE, code=b"\x00\x00\x01") + record(SLICE)
        )
        assert await read_video(response, len(expected)) == expected
        # A keyframe record that carries its own parameter sets leaves in the same form.
        camera.send(record(PPS, SPS, b"\x06sei", IDR))
        again = record(SPS, PPS, b"\x06sei", IDR)
        assert await read_video(response, len(again)) == again
    assert len(camera.sessions) == 1
    assert (served.records, served.keyframes, served.sessions) == (7, 2, 1)


async def test_keyframe_without_known_parameter_sets_is_not_a_start(source):
    served, camera, http = source
    async with http.get(await served.url()) as response:
        camera.send(record(IDR))
        camera.send(record(SLICE))
        camera.send(record(SPS))
        camera.send(record(IDR))  # Still no PPS.
        assert served.keyframes == 0
        camera.send(record(PPS, IDR))
        assert await read_video(response, len(record(SPS, PPS, IDR))) == record(SPS, PPS, IDR)


async def test_readers_share_one_session_and_each_starts_at_a_keyframe(source):
    served, camera, http = source
    url = await served.url()
    async with http.get(url) as first:
        camera.send(record(SPS, PPS, IDR))
        await read_video(first, len(record(SPS, PPS, IDR)))
        async with http.get(url) as second:
            await until(lambda: served.readers == 2)
            camera.send(record(SLICE))
            camera.send(record(IDR))
            expected = record(SLICE) + record(SPS, PPS, IDR)
            assert await read_video(first, len(expected)) == expected
            assert await read_video(second, len(record(SPS, PPS, IDR))) == record(SPS, PPS, IDR)
    assert len(camera.sessions) == 1


async def test_session_outlives_a_brief_gap_then_closes_when_idle(source, monkeypatch):
    served, camera, http = source
    monkeypatch.setattr(tend_source, "IDLE_SECONDS", 0.15)
    url = await served.url()
    async with http.get(url):
        await until(lambda: served.readers == 1)
    await until(lambda: served.readers == 0)
    async with http.get(url):
        await until(lambda: served.readers == 1)
    await until(lambda: served.readers == 0)
    assert len(camera.sessions) == 1 and not camera.sessions[0].closed
    await until(lambda: camera.sessions[0].closed)
    await until(lambda: camera.released == camera.sessions)
    assert served.session is None and served.last_error is None
    assert camera.stopped == []  # Closed for being idle: nothing went wrong.
    async with http.get(url):
        await until(lambda: len(camera.sessions) == 2)


async def test_failed_start_is_refused_and_not_repeated_while_players_retry(source, monkeypatch):
    served, camera, http = source
    camera.error = CannotConnect("Tend legacy video failed or timed out")
    url = await served.url()
    attempts = 0
    original = camera.open

    async def counted(on_video, on_audio):
        nonlocal attempts
        attempts += 1
        return await original(on_video, on_audio)

    served._open_session = counted
    for _ in range(3):
        async with http.get(url) as response:
            assert response.status == 503
            assert await response.text() == "Tend legacy video failed or timed out"
    assert attempts == 1 and served.readers == 0
    assert served.status()["last_error"] == "Tend legacy video failed or timed out"
    camera.error = None
    monkeypatch.setattr(tend_source, "RETRY_SECONDS", 0)
    served._retry_at = 0
    async with http.get(url) as response:
        assert response.status == 200
    assert attempts == 2 and served.last_error is None


async def test_start_is_bounded_and_a_rejection_is_reported(source, monkeypatch):
    served, camera, http = source
    monkeypatch.setattr(tend_source, "START_TIMEOUT", 0.05)
    camera.gate = asyncio.Event()
    url = await served.url()
    async with http.get(url) as response:
        assert response.status == 503
        assert "did not start sending video in time" in await response.text()
    assert not camera.sessions
    served._retry_at = 0
    camera.gate = None
    camera.error = CommandRejected("This camera is unavailable for live video")
    async with http.get(url) as response:
        assert response.status == 503
        assert await response.text() == "This camera is unavailable for live video"


async def test_camera_session_ending_closes_every_reader_and_pauses_restarts(source):
    served, camera, http = source
    url = await served.url()
    async with http.get(url) as response:
        camera.send(record(SPS, PPS, IDR))
        await read(response, PACKET_SIZE)
        camera.sessions[0].ended.set_exception(CannotConnect("Tend camera stopped sending video"))
        async with asyncio.timeout(2):
            await response.content.read()  # Reads to the end of the stream.
    await until(lambda: camera.released == camera.sessions)
    assert camera.sessions[0].closed and served.session is None
    assert served.last_error == "Tend camera stopped sending video"
    assert camera.stopped == ["Tend camera stopped sending video"]
    async with http.get(url) as response:
        assert response.status == 503  # Players reconnect at once; the camera is not re-opened.
    assert len(camera.sessions) == 1


async def test_stop_ends_video_but_keeps_the_address_and_close_removes_it(source):
    served, camera, http = source
    url = await served.url()
    async with http.get(url) as response:
        camera.send(record(SPS, PPS, IDR))
        await read(response, PACKET_SIZE)
        await served.stop()
        async with asyncio.timeout(2):
            await response.content.read()
    assert camera.sessions[0].closed and camera.released == camera.sessions
    assert camera.stopped == []  # Stopped when asked: not reported as a failure.
    async with http.get(url):
        await until(lambda: len(camera.sessions) == 2)
    async with http.get(url.replace(".ts", ".other")) as response:
        assert response.status == 404
    await asyncio.gather(served.close(), served.close())
    assert camera.sessions[1].closed and len(camera.released) == 2
    with pytest.raises(aiohttp.ClientConnectorError):
        async with http.get(url):
            pass
    with pytest.raises(CannotConnect, match="closed"):
        await served.url()


async def test_start_outlives_a_player_that_gives_up_and_its_retry_joins_it(source, monkeypatch):
    """Home Assistant's stream worker gives up after 30 seconds; a start may take longer.

    Tied to the first request, every retry began the camera login again from nothing.
    """
    served, camera, http = source
    monkeypatch.setattr(tend_source, "IDLE_SECONDS", 0.2)
    camera.gate = asyncio.Event()
    url = await served.url()
    first = asyncio.create_task(http.get(url))
    await until(lambda: served.status()["starting"])
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    await asyncio.sleep(0.05)  # The server notices the closed connection.
    assert served.status()["starting"] and not camera.sessions
    retry = asyncio.create_task(http.get(url))
    await asyncio.sleep(0.05)
    camera.gate.set()
    async with asyncio.timeout(2):
        response = await retry
    assert response.status == 200 and len(camera.sessions) == 1
    assert served.last_error is None
    response.close()
    # With nobody reading, the session closes once the idle period passes.
    await until(lambda: camera.sessions[0].closed and camera.released == camera.sessions)


async def test_session_nobody_waited_for_is_closed_when_idle(source, monkeypatch):
    served, camera, http = source
    monkeypatch.setattr(tend_source, "IDLE_SECONDS", 0.05)
    camera.gate = asyncio.Event()
    request = asyncio.create_task(http.get(await served.url()))
    await until(lambda: served.status()["starting"])
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    await asyncio.sleep(0.05)
    camera.gate.set()  # The camera answers after its only player has gone.
    await until(lambda: len(camera.sessions) == 1 and camera.sessions[0].closed)
    await until(lambda: camera.released == camera.sessions and served.session is None)


async def test_reader_cancelled_as_the_start_completes_does_not_orphan_the_session(
    source, monkeypatch
):
    """The session must be owned the moment it exists, whoever was waiting for it."""
    served, camera, http = source
    monkeypatch.setattr(tend_source, "IDLE_SECONDS", 0.05)
    camera.gate = asyncio.Event()
    reader = asyncio.create_task(served._subscribe())
    await asyncio.sleep(0.01)
    camera.gate.set()
    await asyncio.sleep(0)  # The start finishes in this turn of the loop...
    reader.cancel()  # ...and the reader is cancelled before it resumes.
    await asyncio.gather(reader, return_exceptions=True)
    await until(lambda: len(camera.sessions) == 1)
    assert served.session is camera.sessions[0] or camera.sessions[0].closed
    await until(lambda: camera.sessions[0].closed and camera.released == camera.sessions)
    assert served.readers == 0


async def test_defect_during_a_start_is_reported_once_and_not_retried_every_second(source, caplog):
    served, camera, http = source
    attempts = 0

    async def broken(on_video, on_audio):
        nonlocal attempts
        attempts += 1
        raise ValueError("a defect, with details that stay in the log")

    served._open_session = broken
    url = await served.url()
    for _ in range(3):
        async with http.get(url) as response:
            assert response.status == 503
            assert await response.text() == "The camera stream could not start"
    assert attempts == 1 and served.last_error == "The camera stream could not start"
    assert "Unexpected error while starting camera video" in caplog.text


async def test_stop_can_refuse_readers_for_a_while(source):
    """While the camera is being turned off, a reconnecting player must not reopen it."""
    served, camera, http = source
    url = await served.url()
    async with http.get(url) as response:
        await until(lambda: served.readers == 1)
        await served.stop(pause=30)
        async with asyncio.timeout(2):
            await response.content.read()
    async with http.get(url) as response:
        assert response.status == 503
    assert len(camera.sessions) == 1 and served.last_error is None
    served._retry_at = 0
    async with http.get(url) as response:
        assert response.status == 200 and len(camera.sessions) == 2


async def test_session_that_never_sends_a_playable_keyframe_ends_and_says_so(source, monkeypatch):
    """Packets arrive, yet nothing a player can start from: say so instead of hanging."""
    served, camera, http = source
    monkeypatch.setattr(tend_source, "KEYFRAME_SECONDS", 0.15)
    async with http.get(await served.url()) as response:
        await until(lambda: served.readers == 1)
        for _ in range(4):
            camera.send(b"\x65\x88 a keyframe without a start code")
            camera.send(record(SLICE))
            await asyncio.sleep(0.05)
        async with asyncio.timeout(2):
            assert await response.content.read() == b""
    await until(lambda: camera.sessions[0].closed)
    assert served.last_error == "The camera sent no playable keyframe"
    assert camera.stopped == ["The camera sent no playable keyframe"]
    status = served.status()
    assert status["unframed_records"] == 4 and status["records"] == 4
    assert status["keyframes"] == 0


async def test_playable_session_is_not_ended_by_the_keyframe_deadline(source, monkeypatch):
    served, camera, http = source
    monkeypatch.setattr(tend_source, "KEYFRAME_SECONDS", 0.05)
    async with http.get(await served.url()) as response:
        camera.send(record(SPS, PPS, IDR))
        await read_video(response, len(record(SPS, PPS, IDR)))
        await asyncio.sleep(0.2)  # Several deadlines pass with no further keyframe.
        camera.send(record(SLICE))
        assert await read_video(response, len(record(SLICE))) == record(SLICE)
    assert not camera.sessions[0].closed and served.last_error is None


def test_every_parameter_set_in_use_leads_each_keyframe():
    """A camera may use several picture parameter sets; a slice names the one it needs."""
    served = TendSource(None, None)
    reader = _Reader()
    served._readers.add(reader)
    sps = bytes.fromhex("6742001e95a8280f0044")  # seq_parameter_set_id 0
    first, second = bytes.fromhex("68ce3c80"), bytes.fromhex("6848e3c8")  # ids 0 and 1
    served._record(record(sps, first, second, IDR))
    assert video(reader.queue.get_nowait()) == record(sps, first, second, IDR)
    # A changed set replaces the one with the same id, and only that one.
    replaced = bytes.fromhex("6848e3c9")
    served._record(record(replaced))
    served._record(record(IDR))
    assert video(reader.queue.get_nowait()) == record(sps, first, replaced, IDR)


def test_reader_backlog_is_limited_by_size_as_well_as_count(monkeypatch):
    # Tables and a keyframe take three packets, a small picture one.
    monkeypatch.setattr(tend_source, "MAX_PENDING_BYTES", 4 * PACKET_SIZE + 50)
    served = TendSource(None, None)
    reader = _Reader()
    served._readers.add(reader)
    served._record(record(SPS, PPS, IDR))
    served._record(record(b"\x41\x9a" + b"\xff" * 60))
    assert reader.queue.qsize() == 2 and reader.started
    served._record(record(b"\x41\x9a" + b"\xff" * 60))  # Would pass the limit.
    assert reader.queue.qsize() == 0 and reader.size == 0 and not reader.started
    served._record(record(IDR))
    assert reader.started and reader.queue.qsize() == 1


async def test_reader_leaving_right_after_the_start_still_ends_the_session(source, monkeypatch):
    served, camera, http = source
    monkeypatch.setattr(tend_source, "IDLE_SECONDS", 0.05)
    camera.gate = asyncio.Event()
    url = await served.url()
    request = asyncio.create_task(http.get(url))
    await asyncio.sleep(0.05)
    camera.gate.set()  # The session opens just as the reader gives up.
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    await until(lambda: len(camera.sessions) == 1 and camera.sessions[0].closed)
    await until(lambda: camera.released == camera.sessions and served.readers == 0)


async def test_stop_ends_a_start_in_flight_and_refuses_readers_queued_behind_it(source):
    """Turning the camera off, or unloading, must not wait out a slow camera start."""
    served, camera, http = source
    camera.gate = asyncio.Event()  # The camera never answers.
    url = await served.url()
    first = asyncio.create_task(http.get(url))
    await until(lambda: served._session_lock.locked())
    second = asyncio.create_task(http.get(url))
    await asyncio.sleep(0.05)  # The second reader is waiting behind the first's start.
    async with asyncio.timeout(1):
        await served.stop()
    for request in (first, second):
        async with asyncio.timeout(1):
            response = await request
        assert response.status == 503
        assert await response.text() == "The camera stream was stopped"
    assert not camera.sessions and served.readers == 0 and served.session is None
    # A deliberate stop is not a camera failure: the next reader starts at once.
    assert served.last_error is None
    camera.gate = None
    async with http.get(url) as response:
        assert response.status == 200 and len(camera.sessions) == 1


async def test_only_get_opens_the_camera(source):
    served, camera, http = source
    url = await served.url()
    async with http.head(url) as response:
        assert response.status == 405
    async with http.post(url) as response:
        assert response.status == 405
    assert not camera.sessions


async def test_reader_that_stops_reading_is_disconnected(source, monkeypatch):
    served, camera, http = source
    monkeypatch.setattr(tend_source, "WRITE_TIMEOUT", 0.2)
    address = urlsplit(await served.url())
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    try:
        writer.write(f"GET {address.path} HTTP/1.1\r\nHost: {address.netloc}\r\n\r\n".encode())
        await writer.drain()
        await until(lambda: served.readers == 1)
        # More than the socket buffers hold, to a client that never reads.
        camera.send(record(SPS, PPS, IDR))
        async with asyncio.timeout(5):
            while served.readers:
                camera.send(record(b"\x41\x9a" + b"\xff" * (256 * 1024)))
                await asyncio.sleep(0.001)
        # The connection was dropped rather than left to wait on the reader. What the
        # client had not yet read is discarded with it.
        async with asyncio.timeout(5):
            try:
                while await reader.read(1 << 20):
                    pass
            except ConnectionError:
                pass
    finally:
        writer.close()
    # A reader that keeps up is unaffected.
    async with http.get(await served.url()) as response:
        camera.send(record(SPS, PPS, IDR))
        assert await read_video(response, len(record(SPS, PPS, IDR))) == record(SPS, PPS, IDR)
    assert len(camera.sessions) == 1


def test_reader_that_falls_behind_restarts_at_the_next_keyframe(monkeypatch):
    monkeypatch.setattr(tend_source, "MAX_PENDING", 3)
    served = TendSource(None, None)
    reader = _Reader()
    served._readers.add(reader)
    served._record(record(SPS, PPS, IDR))
    served._record(record(SLICE))
    served._record(record(SLICE))
    assert reader.queue.qsize() == 3 and reader.started
    served._record(record(SLICE))  # No room: everything queued is dropped.
    assert reader.queue.qsize() == 0 and not reader.started
    served._record(record(SLICE))
    assert reader.queue.qsize() == 0
    served._record(record(IDR))
    assert reader.started and video(reader.queue.get_nowait()) == record(SPS, PPS, IDR)
    for _ in range(3):
        served._record(record(SLICE))
    served._record(record(IDR))  # A keyframe is itself a place to restart.
    assert reader.started and reader.queue.qsize() == 1
    reader.end()
    reader.end()
    served._record(record(IDR))
    first, last = (reader.queue.get_nowait() for _ in range(2))
    assert video(first) == record(SPS, PPS, IDR) and last == b""


async def test_status_reports_counts_and_no_addresses(source):
    served, camera, http = source
    assert served.status() == {
        "address_served": False,
        "session_open": False,
        "starting": False,
        "readers": 0,
        "sessions_started": 0,
        "records": 0,
        "unframed_records": 0,
        "keyframes": 0,
        "readers_started": 0,
        "readers_with_audio": 0,
        "audio_packets": 0,
        "audio_codec": None,
        "audio_dropped": 0,
        "audio_started_after": None,
        "media_path": None,
        "camera_packets": None,
        "unreadable_packets": None,
        "transport": None,
        "last_error": None,
    }
    url = await served.url()
    async with http.get(url):
        await until(lambda: served.readers == 1)
        media = {"path": "local", "packets": 9, "unreadable": 1}
        camera.sessions[0].media = type("Media", (), media)
        status = served.status()
        assert status["address_served"] and status["session_open"] and status["readers"] == 1
        assert (status["media_path"], status["camera_packets"], status["unreadable_packets"]) == (
            "local",
            9,
            1,
        )
        assert urlsplit(url).path not in str(status) and str(urlsplit(url).port) not in str(status)


def encoded_pictures(count=30):
    """Real H.264 access units from libx264, a keyframe every five pictures."""
    import fractions

    import av
    import numpy

    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = 320, 240, "yuv420p"
    encoder.time_base = fractions.Fraction(1, 20)
    encoder.gop_size = 5
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency", "bframes": "0"}
    pictures = []
    for index in range(count):
        image = numpy.full((240, 320, 3), (index * 8) % 256, numpy.uint8)
        frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(format="yuv420p")
        frame.pts = index
        pictures.extend(bytes(packet) for packet in encoder.encode(frame))
    pictures.extend(bytes(packet) for packet in encoder.encode(None))
    assert len(pictures) == count and nal_units(pictures[0])[0][0] & 31 in (6, 7)
    return pictures


def send_as_camera(camera, picture):
    """Each NAL unit as its own record with a 3-byte start code, as a camera may."""
    for unit in nal_units(picture):
        camera.send(b"\x00\x00\x01" + unit)


async def test_served_stream_is_decodable_h264(source):
    """A real encoded stream, split into camera-style records, opens as H.264 in FFmpeg."""
    import av

    served, camera, http = source
    pictures = encoded_pictures()
    url = await served.url()

    def decode():
        with av.open(url, options={"use_wallclock_as_timestamps": "1"}, timeout=5) as container:
            stream = container.streams.video[0]
            assert stream.codec_context.name == "h264"
            sizes = []
            for frame in container.decode(stream):
                sizes.append((frame.width, frame.height))
                if len(sizes) == 20:
                    break
            return sizes

    reading = asyncio.get_running_loop().run_in_executor(None, decode)
    await until(lambda: served.readers == 1)
    # The reader joins mid-stream: delta pictures arrive before the first keyframe.
    for picture in pictures[2:]:
        send_as_camera(camera, picture)
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)
    await served.stop()
    async with asyncio.timeout(10):
        assert await reading == [(320, 240)] * 20


async def test_served_stream_meets_home_assistants_webrtc_player_rules(source):
    """go2rtc reads the stream as MPEG-TS (pkg/magic: sync byte 0x47; pkg/mpegts): the
    PMT it reads first names the streams, and each video PES is one picture, sent to
    the browser as one frame. A stream that breaks these rules plays in no browser."""
    served, camera, http = source
    pictures = encoded_pictures()
    async with http.get(await served.url()) as response:
        for picture in pictures[2:]:
            send_as_camera(camera, picture)
        await served.stop()
        async with asyncio.timeout(2):
            data = await response.content.read()

    # The PAT, then the PMT: H.264 only, as this camera sent no audio.
    assert data[0] == 0x47 and data[1:3] == b"\x40\x00" and data[189:191] == b"\x50\x00"
    stream = parse(data)
    assert stream.streams == {VIDEO_PID: (0x1B, b"")}
    pes = stream.pes[VIDEO_PID]
    # The encoder cut every picture into several slices. From the first keyframe on
    # (picture 5), each picture must still come out as exactly one PES.
    sliced = [len([u for u in nal_units(picture) if u[0] & 31 in (1, 5)]) for picture in pictures]
    assert max(sliced) > 1
    assert len(pes) == len(pictures) - 5
    times = [pts for pts, _ in pes]
    assert times == sorted(times) and len(set(times)) == len(times)
    for index, (_, picture) in enumerate(pes):
        kinds = [unit[0] & 31 for unit in nal_units(picture)]
        slices = [kind for kind in kinds if kind in (1, 5)]
        assert len(slices) == sliced[index + 5]
        if index % 5 == 0:
            assert picture.startswith(START + b"\x67") and kinds[:2] == [7, 8]
            assert set(slices) == {5}
        else:
            assert set(kinds) == {1}
    # Each keyframe is marked as a place to start, behind the tables.
    assert stream.random_access == len(pes[::5])
    assert all(pcr < pts for pcr, (pts, _) in zip(stream.pcr, pes, strict=True))


@pytest.mark.parametrize("codec", [4, 2])
async def test_audio_is_listed_from_the_start_and_silent_until_the_camera_sound(source, codec):
    """go2rtc reads the tables once, when it starts (pkg/mpegts reads the first PMT
    only), then waits up to five seconds for a packet of each stream listed. So a reader
    starts at the first keyframe with the audio listed while the camera's sound is still
    on its way, and silence fills the audio until it arrives."""
    served, camera, http = source
    async with http.get(await served.url()) as response:
        await until(lambda: served.readers == 1)
        camera.sessions[0].audio_pending = True
        camera.send(record(SPS, PPS, IDR))  # A start at once: no waiting for the audio.
        first = await read_until(response, lambda stream: stream.pes.get(AUDIO_PID))
        frame = opus_packets(1)[0] if codec == 4 else b"\xff" * 160
        camera.audio(codec, frame, 1)
        camera.send(record(SLICE))  # The camera's sound is carried now: no more silence.
        rest = await read_until(response, lambda stream: len(stream.pes.get(AUDIO_PID, [])))
    assert first.streams == {
        VIDEO_PID: (0x1B, b""),
        AUDIO_PID: (0x06, b"\x05\x04Opus\x7f\x02\x80\x01"),
    }
    assert first.video == record(SPS, PPS, IDR)
    [(start, quiet)] = first.opus
    assert quiet == SILENCE and start >= first.pes[VIDEO_PID][0][0]
    [(pts, packet)] = rest.opus
    if codec == 4:
        assert packet == frame  # The camera's Opus, as it was sent.
    # The camera's sound continues the silence's timeline.
    assert opus_samples(packet) == 960 and pts == start + 1800
    status = served.status()
    assert status["audio_codec"] == ("opus" if codec == 4 else "g711")
    assert status["audio_packets"] == 1 and status["audio_dropped"] == 0
    assert isinstance(status["audio_started_after"], float) and status["audio_started_after"] >= 0
    assert (status["readers_started"], status["readers_with_audio"]) == (1, 1)


async def test_no_audio_is_listed_once_the_camera_declines_it(source):
    served, camera, http = source
    async with http.get(await served.url()) as response:
        await until(lambda: served.readers == 1)
        camera.sessions[0].audio_pending = False  # Declined before the first keyframe.
        camera.send(record(SPS, PPS, IDR))
        camera.send(record(SLICE))
        stream = await read_stream(response, 4)  # Tables and two pictures: no silence.
    assert stream.streams == {VIDEO_PID: (0x1B, b"")}
    assert AUDIO_PID not in stream.pes and stream.video == record(SPS, PPS, IDR, SLICE)
    status = served.status()
    assert (status["readers_started"], status["readers_with_audio"]) == (1, 0)
    assert status["audio_started_after"] is None


async def test_served_stream_carries_decodable_sound(source):
    """FFmpeg, which Home Assistant's stream worker uses, finds and decodes the audio."""
    import av

    served, camera, http = source
    pictures = encoded_pictures()
    sounds = opus_packets(40)
    url = await served.url()

    def decode():
        with av.open(url, timeout=5) as container:
            names = sorted(stream.codec_context.name for stream in container.streams)
            audio = container.streams.audio[0]
            samples = 0
            for frame in container.decode(audio):
                samples += frame.samples
                if samples >= 10 * 960:
                    break
            return names, audio.codec_context.channels, samples

    reading = asyncio.get_running_loop().run_in_executor(None, decode)
    await until(lambda: served.readers == 1)
    camera.sessions[0].audio_pending = True
    for index, picture in enumerate(pictures):
        send_as_camera(camera, picture)
        camera.audio(4, sounds[index], index)
        await asyncio.sleep(0.02)
    await served.stop()
    async with asyncio.timeout(10):
        names, channels, samples = await reading
    assert names == ["h264", "opus"] and channels == 1 and samples >= 10 * 960

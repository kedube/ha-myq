"""The whole TC video path on loopback, with only the far ends simulated.

A TLS front door and broker, a UDP relay and a camera stand in for Tend's service and
the device. Everything between them and the player is the integration's own code: the
signaling session, the UDP rendezvous and media receiver, the loopback address, the
camera entity, and Home Assistant's stream worker reading that address.

Nothing here contacts MyQ, Tend or a real camera.
"""

import asyncio
import datetime
import ssl
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.components.camera import get_camera_from_entity_id
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api import tend
from custom_components.myq.api.errors import CannotConnect
from custom_components.myq.api.tend import Frame, parse_properties, properties
from custom_components.myq.api.tend_legacy import LegacyTendSession
from custom_components.myq.api.tend_source import TendSource, nal_units
from custom_components.myq.const import DOMAIN
from custom_components.myq.diagnostics import async_get_config_entry_diagnostics

from .test_tend_rtp import candidates, envelope, fragment, rtp, setup
from .test_tend_source import encoded_pictures

BROKER = "b159.broker.tend-us.tendplatform.com"
KEY = b"0123456789abcdef"
INVENTORY = f"owner:camera,AES={KEY.decode()}+LIP=127.0.0.1+GSN=OPENER-1"
PICTURES_PER_SECOND = 20


def certificate(folder, name):
    """A self-signed certificate naming only ``name``, as the front door's does."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    signed = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = folder / f"{name}.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + signed.public_bytes(serialization.Encoding.PEM)
    )
    return path


class CameraPort(asyncio.DatagramProtocol):
    """One of the camera's two UDP ports: answers the viewer's connectivity checks."""

    def __init__(self):
        self.viewer = None
        self.received = []

    def connection_made(self, transport):
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data, addr):
        if data[:4] != b"SDNK":
            self.received.append(data)
        elif data[7] == 3 and self.viewer is None:
            self.viewer = addr
            self.transport.sendto(setup(3, b"\x01\x00"), addr)
            self.transport.sendto(setup(4, b"\x01"), addr)
        elif data[7] == 4:
            self.transport.sendto(setup(5, data[20:21]), addr)


class Relay(asyncio.DatagramProtocol):
    """Tend's relay: tells each of the viewer's ports where the camera's is."""

    def __init__(self, cameras):
        self.cameras = cameras

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data[:4] == b"SDNK" and data[7] == 1:
            self.transport.sendto(candidates(("127.0.0.1", self.cameras[data[23]].port)), addr)


class Service:
    """Tend's front door and broker, the relay, and one camera sending real H.264."""

    def __init__(self):
        self.dialled = []
        self.logins = []
        self.commands = []
        self.logouts = 0
        self.cameras = []
        self.sending = asyncio.Event()
        self.pictures = encoded_pictures(100)
        self.sent = 0
        self._servers = []
        self._transports = []
        self._video = None
        self._clients = set()

    async def start(self, folder, monkeypatch, *, broker_name=tend.HOST):
        loop = asyncio.get_running_loop()
        for _ in range(2):
            transport, port = await loop.create_datagram_endpoint(
                CameraPort, local_addr=("127.0.0.1", 0)
            )
            self._transports.append(transport)
            self.cameras.append(port)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: Relay(self.cameras), local_addr=("127.0.0.1", 0)
        )
        self._transports.append(transport)
        self.relay_port = transport.get_extra_info("sockname")[1]

        ports = {}
        # Every broker presents the front door's certificate, whatever its own name.
        front_door = certificate(folder, tend.HOST)
        presented = front_door if broker_name == tend.HOST else certificate(folder, broker_name)
        for host, chain, broker in ((tend.HOST, front_door, False), (BROKER, presented, True)):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(chain)

            async def serve(reader, writer, broker=broker):
                task = asyncio.current_task()
                self._clients.add(task)
                try:
                    await self._signaling(reader, writer, broker)
                finally:
                    self._clients.discard(task)

            server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context)
            self._servers.append(server)
            ports[host] = server.sockets[0].getsockname()[1]

        # The client trusts the front door's certificate and nothing else.
        trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        trust.load_verify_locations(front_door)
        monkeypatch.setattr(tend, "_tls_context", trust)
        connect = asyncio.open_connection

        async def local(host, port, **options):
            # Tend's names resolve to this machine; the TLS handshake is the real one.
            self.dialled.append((host, port, options.get("server_hostname")))
            return await connect("127.0.0.1", ports[host], **options)

        monkeypatch.setattr(asyncio, "open_connection", local)
        self._video = asyncio.create_task(self._send_video())

    async def _signaling(self, reader, writer, broker):
        try:
            while True:
                frame = await Frame.read(reader)
                if frame.action == 519:
                    self.logins.append(parse_properties(frame.options))
                    if broker:
                        answer = Frame(
                            519,
                            sender=b"S" * 16,
                            recipient=7,
                            data=properties(
                                {"sid": "session", "alias": "owner", "device-info-list": INVENTORY}
                            ),
                        )
                    else:
                        answer = Frame(
                            519,
                            options=b"clear=clear\n",
                            data=f"1=192.0.2.21\\:5104\nredirectHost={BROKER}\n".encode(),
                        )
                    writer.write(answer.encode())
                elif frame.action == 411:
                    command = frame.data.decode()
                    self.commands.append(command)
                    if command.startswith("require-video-send|"):
                        relay = f"url=rtp\\://localhost\\:{self.relay_port}\n".encode()
                        writer.write(
                            Frame(
                                411, options=relay, data=b"accept-video-receive|owner|9|"
                            ).encode()
                        )
                    elif command.startswith("start-video-receive|"):
                        self.sending.set()
                    elif command.startswith("stop-video-receive|"):
                        self.sending.clear()
                elif frame.action == 408:
                    self.logouts += 1
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    async def _send_video(self):
        """Each picture is one camera packet: encrypted, and split across datagrams."""
        media = self.cameras[0]
        while True:
            await self.sending.wait()
            picture = self.pictures[self.sent % len(self.pictures)]
            records = [b"\x00\x00\x01" + unit for unit in nal_units(picture)]
            packet = rtp(
                envelope(*records, key=KEY),
                sequence=self.sent & 0xFFFF,
                timestamp=self.sent * 90000 // PICTURES_PER_SECOND,
            )
            for index in range(0, len(packet), 1200):
                media.transport.sendto(
                    fragment(self.sent + 1, index // 1200, packet[index : index + 1200]),
                    media.viewer,
                )
            self.sent += 1
            await asyncio.sleep(1 / PICTURES_PER_SECOND)

    async def stop(self):
        tasks = [self._video, *self._clients]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        for server in self._servers:
            server.close()
            await server.wait_closed()
        for transport in self._transports:
            transport.close()


@pytest.fixture
async def service(tmp_path, monkeypatch, socket_enabled):
    simulated = Service()
    await simulated.start(tmp_path, monkeypatch)
    yield simulated
    await simulated.stop()


async def until(condition, seconds=5):
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def test_camera_video_reaches_a_local_reader_unchanged(service):
    import av

    released = []

    async def open_session(on_video):
        session = LegacyTendSession({"OPENER-1"})
        session.on_video = on_video
        await session.start("owner:camera", "synthetic-token")
        return session

    async def closed(session):
        released.append(session)

    source = TendSource(open_session, closed)
    url = await source.url()

    def decode():
        with av.open(url, options={"use_wallclock_as_timestamps": "1"}, timeout=10) as container:
            stream = container.streams.video[0]
            sizes = []
            for frame in container.decode(stream):
                sizes.append((stream.codec_context.name, frame.width, frame.height))
                if len(sizes) == 30:
                    break
            return sizes

    try:
        async with asyncio.timeout(20):
            pictures = await asyncio.get_running_loop().run_in_executor(None, decode)
        assert pictures == [("h264", 320, 240)] * 30

        # The front door, then the broker it named; both proved the front door's name.
        assert service.dialled == [(tend.HOST, 5104, tend.HOST), (BROKER, 5104, tend.HOST)]
        assert [login["CX_PASSWD"] for login in service.logins] == [
            "myQ.accessToken:synthetic-token"
        ] * 2
        assert service.commands == [
            "require-video-send|owner|9|",
            "start-video-receive|owner|9|",
            "modify-device|owner|QTY=3+FPS=20+SIZE=0+SAVE=0|",
        ]
        status = source.status()
        assert status["session_open"] and status["sessions_started"] == 1
        # The simulated relay and camera share one address, which reads as the relay.
        assert status["media_path"] == "relay"
        assert status["camera_packets"] >= 30 and status["unreadable_packets"] == 0
        assert status["keyframes"] >= 6 and status["last_error"] is None
        # The camera's control port hears receiver reports while video flows.
        await until(lambda: any(data[1:2] == b"\xc9" for data in service.cameras[1].received))
    finally:
        await source.close()

    assert service.commands[-1] == "stop-video-receive|owner|9|"
    await until(lambda: service.logouts == 1)
    assert not service.sending.is_set() and len(released) == 1
    # The control port was told goodbye (RTCP BYE).
    await until(lambda: any(data[1:2] == b"\xcb" for data in service.cameras[1].received))


async def test_broker_without_the_front_doors_certificate_is_refused(
    tmp_path, monkeypatch, socket_enabled
):
    """The access token is only ever sent to a host holding the front door's certificate."""
    simulated = Service()
    await simulated.start(tmp_path, monkeypatch, broker_name=BROKER)
    session = LegacyTendSession({"OPENER-1"})
    try:
        with pytest.raises(CannotConnect):
            await session.start("owner:camera", "synthetic-token")
        # Only the front door ever received a login.
        assert len(simulated.logins) == 1 and not simulated.commands
    finally:
        await session.close()
        await simulated.stop()


@pytest.fixture
async def home_camera(hass, enable_custom_integrations, tokens, camera_device, camera_details):
    """A TC camera entity in Home Assistant with the real camera session class."""
    camera_device = replace(
        camera_device,
        serial="TC-TEST-CAMERA",
        state={"online": True, "paired_gdo_serial_number": "OPENER-1"},
    )
    camera_details["alias"] = "owner:camera"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-tend-end-to-end",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={camera_device.key: camera_device}),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(return_value=camera_details),
        ),
        patch(
            "custom_components.myq.api.video.Video.wake_best_effort",
            AsyncMock(return_value=camera_details),
        ),
    ):
        assert await async_setup_component(hass, "stream", {"stream": {}})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity = get_camera_from_entity_id(hass, hass.states.async_all("camera")[0].entity_id)
        yield SimpleNamespace(camera=entity, entry=entry)
        if entry.state.recoverable:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_home_assistant_plays_the_camera_through_its_stream_worker(
    hass, home_camera, service
):
    """Home Assistant's HLS player: its worker reads the address and cuts segments."""
    camera = home_camera.camera
    stream = await camera.async_create_stream()
    assert stream is not None and stream.source == await camera.stream_source()
    output = stream.add_provider("hls")
    await stream.start()

    async with asyncio.timeout(30):
        while not (segments := [item for item in output.get_segments() if item.complete]):
            assert await output.recv()
    segment = segments[0]
    assert segment.init and segment.get_data()
    # Home Assistant's default (low-latency HLS) cuts six-second segments into parts.
    # The duration comes from wall-clock timestamps: it matches the time video took
    # to arrive, so the player neither stalls nor races.
    assert 5 < segment.duration < 8 and len(segment.parts) >= 5
    details = stream.get_diagnostics()
    assert details["container_format"] == "h264" and details["video_codec"] == "h264"
    assert stream.available

    data = await async_get_config_entry_diagnostics(hass, home_camera.entry)
    video = data["devices"][0]["live_video_source"]
    assert video["session_open"] and video["readers"] == 1 and video["media_path"] == "relay"
    assert video["unreadable_packets"] == 0 and video["last_error"] is None
    assert service.logins[-1]["CX_PASSWD"] == "myQ.accessToken:test-access"

    # Unloading ends the worker, the camera session and the camera's sending.
    assert await hass.config_entries.async_unload(home_camera.entry.entry_id)
    await hass.async_block_till_done()
    assert camera.stream is None
    await until(lambda: service.logouts == 1)
    assert service.commands[-1] == "stop-video-receive|owner|9|"
    assert not service.sending.is_set()

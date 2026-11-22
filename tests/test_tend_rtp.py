"""Synthetic mode-9 packets and a loopback relay; no camera or cloud access."""

import asyncio
import socket
import struct
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from custom_components.myq.api import tend_rtp
from custom_components.myq.api.errors import CannotConnect, ProtocolError
from custom_components.myq.api.tend_rtp import TendRtp, _Port, _rtp_body, _video_records

KEY = bytes(range(16))
SPS = bytes.fromhex("000000016742001e95a8280f0044")
IDR = bytes.fromhex("00000165b8041014")


def envelope(*frames, key=None):
    content = b"".join(len(frame).to_bytes(4, "little") + frame for frame in frames)
    flags = bytes(4)
    if key is not None:
        iv = bytes(range(16))
        padder = PKCS7(128).padder()
        padded = padder.update(content) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        content = iv + encryptor.update(padded) + encryptor.finalize()
        flags = b"\x20\0\0\0"
    return b"w1" + (len(content) + 4).to_bytes(4, "little") + flags + content


def rtp(payload, *, sequence=1, timestamp=90000, source=42):
    return struct.pack("!BBHII", 128, 96, sequence, timestamp, source) + payload


def fragment(group, index, payload):
    return group.to_bytes(4, "little") + index.to_bytes(4, "little") + payload


def setup(kind, payload):
    header = bytearray(20)
    header[:8] = b"SDNK\x01\x00\x00" + bytes([kind])
    header[16:20] = len(payload).to_bytes(4, "big")
    return bytes(header) + payload


def candidates(*peers):
    entries = b"".join(
        socket.inet_aton(address) + bytes(16) + port.to_bytes(2, "big") for address, port in peers
    )
    return setup(2, entries.ljust(90, b"\0"))


def prepared_receiver():
    receiver = TendRtp()
    receiver._key = KEY
    receiver._relay = ("192.0.2.1", 5100)
    receiver._identity = b"V_test".ljust(32, b"\0")
    for index in range(2):
        port = _Port(receiver, index)
        port.transport = MagicMock()
        port.transport.is_closing.return_value = False
        receiver._ports.append(port)
    return receiver


def test_plain_and_encrypted_records_and_invalid_padding():
    assert _video_records(envelope(SPS, IDR), b"") == [SPS, IDR]
    encrypted = envelope(SPS, IDR, key=KEY)
    assert _video_records(encrypted, KEY) == [SPS, IDR]
    assert _video_records(encrypted, b"") == []
    assert _video_records(encrypted[:-1] + bytes([encrypted[-1] ^ 128]), KEY) == []
    assert _video_records(encrypted, b"different-key-12") == []


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"w1" + bytes(8),
        b"xx" + bytes(8),
        envelope(IDR)[:-1],
        envelope(b"not-an-annex-b-unit"),
        # A valid record followed by a truncated one must not emit a partial packet.
        b"w1"
        + (4 + 4 + len(IDR) + 5).to_bytes(4, "little")
        + bytes(4)
        + len(IDR).to_bytes(4, "little")
        + IDR
        + b"\x09\0\0\0x",
    ],
)
def test_invalid_envelopes_are_discarded(payload):
    assert _video_records(payload, KEY) == []


def test_rtp_extension_csrc_and_padding():
    payload = envelope(IDR)
    header = bytearray(rtp(b""))
    header[0] = 0xB1  # padding, extension, one CSRC
    packet = bytes(header) + bytes(4) + b"\xbe\xde\0\x01" + bytes(4) + payload + b"\0\0\0\x04"
    assert _rtp_body(packet) == payload
    assert _rtp_body(bytes(header)) is None
    assert _rtp_body(packet[:-1] + b"\0") is None
    assert _rtp_body(packet[:12] + bytes(4) + b"\xbe\xde\xff\xff") is None


async def test_fragments_duplicates_unknown_metadata_and_old_replays():
    receiver = TendRtp()
    packet = rtp(envelope(SPS, IDR))
    pieces = [packet[:9], packet[9:22], packet[22:]]
    for index in (0, 1, 2, 2, 0):
        receiver._fragment(fragment(70, 0xA1000000 + index, pieces[index]))
    assert len(receiver._parts) == 3
    assert receiver._part_bytes == len(packet)
    receiver._fragment(fragment(71, 0, rtp(envelope(IDR), sequence=2)))
    assert await receiver.recv() == SPS
    assert await receiver.recv() == IDR
    receiver._fragment(fragment(70, 0, pieces[0]))
    assert receiver._group == 71
    receiver._fragment(fragment(72, 0, b"next"))
    assert await receiver.recv() == IDR
    await receiver.close()


async def test_duplicate_flood_missing_fragments_and_oversize_groups_are_bounded(monkeypatch):
    receiver = TendRtp()
    packet = rtp(envelope(IDR))
    first = fragment(1, 0, packet[:12])
    for _ in range(1000):
        receiver._fragment(first)
    assert receiver._part_bytes == 12
    receiver._fragment(fragment(1, 2, packet[13:]))
    receiver._fragment(fragment(2, 0, packet[:12]))
    assert receiver._queue.empty()  # A missing byte invalidated the media envelope.
    monkeypatch.setattr(tend_rtp, "MAX_PACKET", 20)
    receiver._fragment(fragment(3, 0, packet))
    assert receiver._part_bytes == 0
    monkeypatch.setattr(tend_rtp, "MAX_FRAGMENTS", 1)
    receiver._fragment(fragment(4, 0, b"first"))
    receiver._fragment(fragment(4, 1, b"second"))
    assert receiver._part_bytes == 0
    await receiver.close()


async def test_candidates_require_exact_relay_and_peer_is_pinned():
    receiver = prepared_receiver()
    port = receiver._ports[0]
    first = ("192.0.2.10", 6000)
    second = ("192.0.2.10", 6002)
    reply = candidates(first, second)
    receiver._receive(port, reply, (receiver._relay[0], receiver._relay[1] + 1))
    assert not port.candidates
    receiver._receive(port, reply[:-1], receiver._relay)
    assert not port.candidates
    receiver._receive(port, reply, receiver._relay)
    assert port.candidates == (first, second)
    receiver._receive(port, setup(5, b"\x01"), ("192.0.2.99", 6000))
    assert not port.ready
    receiver._receive(port, setup(4, b"\x01"), first)
    assert port.peer == first
    receiver._receive(port, setup(5, b"\x01"), second)
    assert not port.ready
    receiver._receive(port, setup(5, b"\x01"), first)
    assert port.ready
    assert not receiver._ready.is_set()  # Both channels are required.
    receiver._receive(port, fragment(1, 0, rtp(envelope(IDR))), second)
    assert not receiver._parts
    receiver._receive(port, fragment(1, 0, rtp(envelope(IDR))), first)
    receiver._receive(port, fragment(2, 0, b"next"), first)
    assert await receiver.recv() == IDR
    await receiver.close()


async def test_close_unblocks_pending_reader_and_clears_keys():
    receiver = prepared_receiver()
    read = asyncio.create_task(receiver.recv())
    await asyncio.sleep(0)
    await receiver.close()
    with pytest.raises(CannotConnect, match="closed"):
        await read
    await receiver.close()
    await receiver.wait_closed()
    assert not receiver._identity and not receiver._key
    assert all(port.transport.close.called for port in receiver._ports)


async def test_receiver_timeout_and_queue_limits(monkeypatch):
    monkeypatch.setattr(tend_rtp, "FRAME_TIMEOUT", 0.01)
    receiver = TendRtp()
    with pytest.raises(CannotConnect, match="stopped sending"):
        await receiver.recv()
    with pytest.raises(CannotConnect, match="stopped sending"):
        await receiver.wait_closed()
    await receiver.close()

    monkeypatch.setattr(tend_rtp, "MAX_QUEUED_BYTES", len(IDR) - 1)
    receiver = TendRtp()
    receiver._fragment(fragment(1, 0, rtp(envelope(IDR))))
    receiver._fragment(fragment(2, 0, b"next"))
    with pytest.raises(CannotConnect, match="not keeping up"):
        await receiver.recv()
    await receiver.close()


async def test_receiver_report_sequence_wrap_loss_and_sender_report():
    receiver = TendRtp()
    receiver._observe(rtp(b"", sequence=65535, timestamp=0))
    receiver._observe(rtp(b"", sequence=1, timestamp=6000))
    sender_report = struct.pack("!BBHIIIIII", 128, 200, 6, 42, 0x12345678, 0x9ABCDEF0, 6000, 2, 200)
    receiver._rtcp(sender_report)
    report = receiver._report()
    assert report is not None and len(report) == 32
    assert report[:4] == b"\x81\xc9\0\x07"
    assert report[12] == 85
    assert int.from_bytes(report[13:16], "big") == 1
    assert int.from_bytes(report[16:20], "big") == 65537
    assert int.from_bytes(report[24:28], "big") == 0x56789ABC
    await receiver.close()


class CameraPeer(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.viewer = None
        self.bye = asyncio.Event()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.startswith(b"SDNK") and data[7] == 4:
            self.viewer = addr
            self.transport.sendto(setup(5, b"\x01"), addr)
        elif len(data) >= 8 and data[1] == 203:
            self.bye.set()


class RelayPeer(asyncio.DatagramProtocol):
    def __init__(self, cameras):
        self.cameras = cameras
        self.transport = None
        self.registrations = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.startswith(b"SDNK") and data[7] == 1:
            self.registrations.append(data)
            index = data[23]
            peer = self.cameras[index].transport.get_extra_info("sockname")
            self.transport.sendto(candidates(peer), addr)


async def test_real_loopback_handshake_encrypted_video_and_cleanup(socket_enabled):
    loop = asyncio.get_running_loop()
    cameras = [CameraPeer(), CameraPeer()]
    transports = []
    receiver = TendRtp()
    try:
        for camera in cameras:
            transport, _ = await loop.create_datagram_endpoint(
                lambda camera=camera: camera, local_addr=("127.0.0.1", 0)
            )
            transports.append(transport)
        relay = RelayPeer(cameras)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: relay, local_addr=("127.0.0.1", 0)
        )
        transports.append(transport)
        await receiver.start(transport.get_extra_info("sockname"), "test-account", "127.0.0.1", KEY)
        assert len(relay.registrations) == 2
        for index, packet in enumerate(relay.registrations):
            assert packet[:8] == b"SDNK\x01\0\0\x01"
            assert packet[20:25] == bytes([1, 0, 0, index]) + b"V"
            assert packet[-64:-32] == b"V_test-account".ljust(32, b"\0")
            assert packet[-32:] == packet[-64:-32]
        assert receiver._ports[1].number == receiver._ports[0].number + 1
        assert receiver._ready.is_set()
        packet = rtp(envelope(SPS, IDR, key=KEY))
        camera = cameras[0]
        camera.transport.sendto(fragment(1, 0, packet[:23]), camera.viewer)
        camera.transport.sendto(fragment(1, 1, packet[23:40]), camera.viewer)
        camera.transport.sendto(fragment(1, 2, packet[40:]), camera.viewer)
        camera.transport.sendto(fragment(2, 0, b"next frame"), camera.viewer)
        async with asyncio.timeout(1):
            assert await receiver.recv() == SPS
            assert await receiver.recv() == IDR
        await receiver.close()
        async with asyncio.timeout(1):
            await cameras[1].bye.wait()
        assert receiver._maintenance is None
        with pytest.raises(ProtocolError, match="cannot be reused"):
            await receiver.start(("127.0.0.1", 5104), "account", "", KEY)
    finally:
        await receiver.close()
        for transport in transports:
            transport.close()
        await asyncio.sleep(0)


async def test_cancelled_setup_releases_sockets(socket_enabled, monkeypatch):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    receiver = TendRtp()
    bound = asyncio.Event()
    original_bind = receiver._bind

    async def bind():
        await original_bind()
        bound.set()

    monkeypatch.setattr(receiver, "_bind", bind)
    task = asyncio.create_task(
        receiver.start(transport.get_extra_info("sockname"), "account", "", KEY)
    )
    try:
        async with asyncio.timeout(1):
            await bound.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(port.transport.is_closing() for port in receiver._ports)
        assert receiver._ended.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await receiver.close()
        transport.close()

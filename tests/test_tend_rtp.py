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
from custom_components.myq.api.tend_rtp import TendRtp, _candidates, _Port, _video_records

KEY = bytes(range(16))
SPS = bytes.fromhex("000000016742001e95a8280f0044")
IDR = bytes.fromhex("00000165b8041014")
RELAY = ("192.0.2.1", 5100)
CAMERA = ("192.168.1.20", 4000)
# Not a documentation range: Python counts those as private, as the selection does.
PUBLIC = ("93.184.216.34", 4100)


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


def rtp(payload, *, sequence=1, timestamp=90000, source=42, first=128):
    return struct.pack("!BBHII", first, 96, sequence, timestamp, source) + payload


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


def prepared_receiver(*, camera_ip=CAMERA[0], key=KEY, confirmed=False):
    """A receiver on fake sockets; ``confirmed`` as after the camera's handshake."""
    records, ended = [], []
    receiver = TendRtp(records.append, ended.append)
    receiver._key = key
    receiver._relay = RELAY
    receiver._camera_ip = camera_ip
    receiver._source_ip = "192.168.1.5"
    receiver._allowed = {RELAY[0]} | ({camera_ip} if camera_ip else set())
    receiver._identity = b"V_test".ljust(32, b"\0")
    for index in range(2):
        port = _Port(receiver, index)
        port.transport = MagicMock()
        port.transport.is_closing.return_value = False
        port.ready = confirmed
        receiver._ports.append(port)
    return receiver, records, ended


def sent(port):
    return [(call.args[0], call.args[1]) for call in port.transport.sendto.call_args_list]


def test_plain_and_encrypted_records_and_invalid_padding():
    assert _video_records(envelope(SPS, IDR), b"") == [SPS, IDR]
    encrypted = envelope(SPS, IDR, key=KEY)
    assert _video_records(encrypted, KEY) == [SPS, IDR]
    assert _video_records(encrypted, b"") == []
    assert _video_records(encrypted[:-1] + bytes([encrypted[-1] ^ 128]), KEY) == []
    assert _video_records(encrypted, b"different-key-12") == []


@pytest.mark.parametrize("payload", [b"", b"w1" + bytes(7), b"xx" + bytes(8), envelope(IDR)[:-1]])
def test_envelopes_with_a_wrong_marker_or_length_are_discarded(payload):
    assert _video_records(payload, KEY) == []


def test_records_are_passed_on_as_sent_and_a_malformed_tail_keeps_earlier_ones():
    # Working clients do not inspect a record's contents, and stop at a bad length.
    odd = b"\x09no-start-code"
    assert _video_records(envelope(odd, IDR), b"") == [odd, IDR]
    body = len(IDR).to_bytes(4, "little") + IDR
    for tail in (b"\x09\0\0\0x", bytes(4), b"\x01"):
        payload = b"w1" + (len(body) + len(tail) + 4).to_bytes(4, "little") + bytes(4) + body + tail
        assert _video_records(payload, b"") == [IDR]


def test_candidate_reply_must_be_the_110_byte_form():
    assert _candidates(candidates(CAMERA, PUBLIC)) == [CAMERA, PUBLIC]
    assert _candidates(candidates(CAMERA)[:-1]) == []
    assert _candidates(candidates(("0.0.0.0", 4000), ("192.168.1.9", 0))) == []


def test_fragments_join_in_arrival_order_and_complete_on_the_next_packet():
    receiver, records, _ = prepared_receiver(key=b"", confirmed=True)
    packet = rtp(envelope(SPS, IDR))
    pieces = [packet[:9], packet[9:22], packet[22:]]
    for index, piece in enumerate(pieces):
        receiver._receive(receiver._ports[0], fragment(70, 0xA1000000 + index, piece), CAMERA)
    assert records == []
    receiver._receive(receiver._ports[0], fragment(71, 0, rtp(envelope(IDR), sequence=2)), CAMERA)
    assert records == [SPS, IDR]
    # Packet numbers are only compared with the one before: a repeat is a new packet.
    receiver._receive(receiver._ports[0], fragment(70, 0, rtp(envelope(SPS), sequence=3)), CAMERA)
    receiver._receive(receiver._ports[0], fragment(72, 0, b"next"), CAMERA)
    assert records == [SPS, IDR, IDR, SPS]
    assert (receiver.packets, receiver.unreadable) == (3, 0)


def test_the_rtp_header_is_a_fixed_twelve_bytes_whatever_its_flag_bits_say():
    receiver, records, _ = prepared_receiver(key=b"")
    # Extension, padding and CSRC bits set: clients that work ignore all three.
    receiver._fragment(fragment(1, 0, rtp(envelope(IDR), first=0xB1)))
    receiver._fragment(fragment(2, 0, b"next-one"))
    assert records == [IDR]


def test_damaged_short_and_oversize_packets_are_dropped_and_counted(monkeypatch):
    receiver, records, _ = prepared_receiver(key=b"")
    packet = rtp(envelope(IDR))
    receiver._fragment(fragment(1, 0, packet[:12]))
    receiver._fragment(fragment(1, 2, packet[13:]))  # A datagram went missing.
    receiver._fragment(fragment(2, 0, b"\x00" * 20))  # Not RTP version 2.
    receiver._fragment(b"short")
    monkeypatch.setattr(tend_rtp, "MAX_PACKET", 40)
    receiver._fragment(fragment(3, 0, packet))
    receiver._fragment(fragment(3, 1, packet[:8]))
    assert receiver._parts == [] and receiver._oversize
    monkeypatch.setattr(tend_rtp, "MAX_PACKET", 2 * 1024 * 1024)
    receiver._fragment(fragment(4, 0, packet))
    receiver._fragment(fragment(5, 0, b"x" * 9))
    assert records == [IDR]
    assert (receiver.packets, receiver.unreadable) == (2, 1)


def test_relay_reply_selects_the_cameras_own_address_then_private_then_last():
    receiver, _, _ = prepared_receiver()
    media = receiver._ports[0]
    receiver._receive(media, candidates(PUBLIC, CAMERA), RELAY)
    assert media.peer == CAMERA
    # The relay is told the choice was made, on the port that made it.
    assert sent(media)[-1] == (setup(100, b"\x01\x00V" + receiver._identity * 2), RELAY)

    receiver, _, _ = prepared_receiver(camera_ip="")
    receiver._receive(
        receiver._ports[1], candidates(PUBLIC, ("192.168.1.5", 9), ("10.0.0.8", 4001)), RELAY
    )
    assert receiver._ports[1].peer == ("10.0.0.8", 4001)  # Never this host's own address.

    receiver, _, _ = prepared_receiver(camera_ip="")
    receiver._receive(receiver._ports[0], candidates(("1.1.1.1", 1), PUBLIC), RELAY)
    assert receiver._ports[0].peer == PUBLIC


def test_only_the_relay_and_announced_camera_addresses_are_heard():
    receiver, records, _ = prepared_receiver(camera_ip="", key=b"")
    media = receiver._ports[0]
    stranger = ("8.8.4.4", 4000)
    receiver._receive(media, candidates(CAMERA), stranger)  # Not the relay.
    receiver._receive(media, setup(5, b"\x01"), stranger)
    receiver._receive(media, fragment(1, 0, rtp(envelope(IDR))), CAMERA)  # Not yet announced.
    assert media.peer is None and not media.ready and receiver._parts == []
    receiver._receive(media, candidates(CAMERA), (RELAY[0], 6000))  # Any relay port.
    assert media.peer == CAMERA
    # Announced, but the camera has not confirmed the port: still nothing is taken.
    receiver._receive(media, fragment(1, 0, rtp(envelope(IDR))), CAMERA)
    receiver._receive(media, fragment(2, 0, b"next-one"), CAMERA)
    assert records == [] and receiver._parts == []
    receiver._receive(media, setup(5, b"\x01"), CAMERA)
    assert media.ready
    receiver._receive(media, fragment(3, 0, rtp(envelope(IDR))), CAMERA)
    receiver._receive(media, fragment(4, 0, b"next-one"), CAMERA)
    assert records == [IDR]


def test_camera_may_answer_from_another_port_and_handshake_replies_follow_it():
    receiver, _, _ = prepared_receiver()
    media, control = receiver._ports
    for port in (media, control):
        receiver._receive(port, candidates(CAMERA), RELAY)
    moved = (CAMERA[0], 4555)
    receiver._receive(media, setup(3, b"\x01\x00"), moved)
    assert media.peer == moved
    assert sent(media)[-3:] == [
        (setup(3, bytes([1, sequence]) + receiver._identity * 2), moved) for sequence in range(3)
    ]
    receiver._receive(media, setup(4, b"\x07"), moved)
    assert sent(media)[-1] == (setup(5, b"\x07"), moved)
    receiver._receive(media, setup(4, b""), moved)  # An empty probe still gets an answer.
    assert sent(media)[-1] == (setup(5, b"\x01"), moved)
    receiver._receive(media, setup(5, b"\x01"), moved)
    assert media.ready and not receiver._ready.is_set()
    assert receiver.path is not None
    receiver._receive(control, setup(5, b"\x01"), CAMERA)
    assert receiver._ready.is_set() and receiver.path == "local"


def test_relay_reply_after_the_camera_confirmed_a_port_does_not_move_it():
    receiver, _, _ = prepared_receiver()
    control = receiver._ports[1]
    receiver._receive(control, candidates(CAMERA, PUBLIC), RELAY)
    assert control.peer == CAMERA
    # The camera answers from its public address instead, and confirms the port.
    receiver._receive(control, setup(5, b"\x01"), PUBLIC)
    assert control.peer == PUBLIC and control.ready
    # A repeated relay reply (one per registration sent) arrives late.
    receiver._receive(control, candidates(CAMERA, PUBLIC), RELAY)
    # Receiver reports and the closing goodbye go to this address.
    assert control.peer == PUBLIC and control.ready


def test_media_may_arrive_through_the_relay():
    receiver, records, _ = prepared_receiver(key=b"")
    for port in receiver._ports:
        receiver._receive(port, candidates(CAMERA), RELAY)
        receiver._receive(port, setup(5, b"\x01"), RELAY)
    assert receiver._ready.is_set() and receiver.path == "relay"
    receiver._receive(receiver._ports[0], fragment(1, 0, rtp(envelope(IDR))), RELAY)
    receiver._receive(receiver._ports[0], fragment(2, 0, b"next-one"), RELAY)
    assert records == [IDR]


async def test_receiver_report_sequence_wrap_loss_and_sender_report():
    receiver, _, _ = prepared_receiver(key=b"", confirmed=True)
    control = receiver._ports[1]
    control.peer = CAMERA
    assert receiver._report() is None
    for group, sequence in enumerate((65534, 65535, 1), start=1):
        receiver._fragment(fragment(group, 0, rtp(envelope(IDR), sequence=sequence)))
    receiver._fragment(fragment(9, 0, b"flush-it"))
    sender_report = struct.pack("!BBHIIIIII", 0x80, 200, 6, 42, 0x11223344, 0x55667788, 0, 0, 0)
    receiver._receive(control, sender_report, CAMERA)
    report = receiver._report()
    assert report is not None and len(report) == 32
    version, kind, words, own, source = struct.unpack_from("!BBHII", report)
    assert (version, kind, words, own, source) == (0x81, 201, 7, receiver._ssrc, 42)
    assert int.from_bytes(report[13:16], "big") == 1  # 65534, 65535, (0 lost), 1
    assert struct.unpack_from("!I", report, 16)[0] == 65537
    assert struct.unpack_from("!I", report, 24)[0] == 0x33445566
    await receiver.close()
    assert sent(control)[-1] == (struct.pack("!BBHI", 0x81, 203, 1, receiver._ssrc), CAMERA)


async def test_silence_ends_the_receiver_and_reports_why(monkeypatch):
    receiver, records, ended = prepared_receiver(confirmed=True)
    monkeypatch.setattr(tend_rtp, "FRAME_TIMEOUT", 0)
    original = asyncio.sleep
    monkeypatch.setattr(tend_rtp.asyncio, "sleep", lambda _: original(0))
    receiver._last_video_at = -1.0
    await receiver._maintain()
    assert len(ended) == 1 and isinstance(ended[0], CannotConnect)
    assert "stopped sending video" in str(ended[0])
    assert receiver._key == b"" and receiver._identity == b""
    receiver._fragment(fragment(1, 0, rtp(envelope(IDR))))
    receiver._receive(receiver._ports[0], fragment(2, 0, b"late-one"), CAMERA)
    assert records == []
    await receiver.close()
    assert len(ended) == 1  # A deliberate close is not reported as a failure.


@pytest.mark.parametrize(
    ("relay", "alias", "key"),
    [
        (("192.0.2.1", 0), "owner", b""),
        (("192.0.2.1", 5100), "", b""),
        (("192.0.2.1", 5100), "owné", b""),
        (("192.0.2.1", 5100), "owner", b"short"),
    ],
)
async def test_invalid_registration_never_opens_sockets(relay, alias, key):
    receiver = TendRtp(lambda record: None)
    with pytest.raises(ProtocolError):
        await receiver.start(relay, alias, "", key)
    assert not receiver._ports
    with pytest.raises(ProtocolError, match="cannot be reused"):
        await receiver.start(("192.0.2.1", 5100), "owner", "", b"")


async def test_real_loopback_handshake_encrypted_video_and_cleanup(socket_enabled):
    loop = asyncio.get_running_loop()
    records: list[bytes] = []
    got_video = asyncio.Event()

    def on_record(record):
        records.append(record)
        got_video.set()

    class Relay(asyncio.DatagramProtocol):
        """Answers each registration with the camera's address for that port."""

        def __init__(self):
            self.registrations = []
            self.selected = []

        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            if data[7] == 1:
                index = data[23]
                self.registrations.append(data)
                self.transport.sendto(candidates(("127.0.0.1", cameras[index].port)), addr)
            elif data[7] == 100:
                self.selected.append(data[20:23])

    class Camera(asyncio.DatagramProtocol):
        def __init__(self):
            self.viewer = None
            self.received = []

        def connection_made(self, transport):
            self.transport = transport
            self.port = transport.get_extra_info("sockname")[1]

        def datagram_received(self, data, addr):
            self.received.append(data)
            if data[:4] != b"SDNK":
                return
            if data[7] == 3 and self.viewer is None:
                self.viewer = addr
                self.transport.sendto(setup(3, b"\x01\x00"), addr)
                self.transport.sendto(setup(4, b"\x01"), addr)
            elif data[7] == 4:
                self.transport.sendto(setup(5, data[20:21]), addr)

    cameras = []
    for _ in range(2):
        _, protocol = await loop.create_datagram_endpoint(Camera, local_addr=("127.0.0.1", 0))
        cameras.append(protocol)
    relay_transport, relay = await loop.create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
    receiver = TendRtp(on_record)
    try:
        await receiver.start(
            ("127.0.0.1", relay_transport.get_extra_info("sockname")[1]), "owner", "127.0.0.1", KEY
        )
        assert receiver.path is not None
        identity = b"V_owner".ljust(32, b"\0")
        registration = relay.registrations[0]
        assert registration[20:25] == b"\x01\x00\x00\x00V"
        assert registration[25:29] == socket.inet_aton("127.0.0.1")
        assert registration[-64:] == identity * 2
        assert sorted(relay.selected) == [b"\x01\x00V", b"\x01\x01V"]
        media = cameras[0]
        packet = rtp(envelope(SPS, IDR, key=KEY))
        media.transport.sendto(fragment(1, 0, packet[:30]), media.viewer)
        media.transport.sendto(fragment(1, 1, packet[30:]), media.viewer)
        following = rtp(envelope(IDR, key=KEY), sequence=2)
        media.transport.sendto(fragment(2, 0, following), media.viewer)
        async with asyncio.timeout(2):
            await got_video.wait()
        assert records == [SPS, IDR]
    finally:
        await receiver.close()
        await asyncio.sleep(0.05)
        relay_transport.close()
        for camera in cameras:
            camera.transport.close()
    # The control port says goodbye; both local sockets are closed.
    assert struct.pack("!BBHI", 0x81, 203, 1, receiver._ssrc) in cameras[1].received
    assert all(port.transport.is_closing() for port in receiver._ports)


async def test_no_camera_answer_times_out_and_releases_sockets(socket_enabled, monkeypatch):
    monkeypatch.setattr(tend_rtp, "CONNECT_TIMEOUT", 0.3)
    loop = asyncio.get_running_loop()
    silent, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    receiver = TendRtp(lambda record: None)
    try:
        with pytest.raises(CannotConnect, match="UDP connection failed or timed out"):
            await receiver.start(
                ("127.0.0.1", silent.get_extra_info("sockname")[1]), "owner", "", b""
            )
    finally:
        silent.close()
    assert len(receiver._ports) == 2
    assert all(port.transport.is_closing() for port in receiver._ports)


async def test_cancelled_setup_releases_sockets(socket_enabled):
    loop = asyncio.get_running_loop()
    silent, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    receiver = TendRtp(lambda record: None)
    task = asyncio.create_task(
        receiver.start(("127.0.0.1", silent.get_extra_info("sockname")[1]), "owner", "", b"")
    )
    try:
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        silent.close()
    assert all(port.transport.is_closing() for port in receiver._ports)


def test_media_kind_is_video_or_audio():
    with pytest.raises(ProtocolError, match="media kind"):
        TendRtp(lambda record: None, kind=b"X")


async def test_audio_connection_registers_with_kind_a():
    receiver = TendRtp(lambda record: None, kind=b"A", watch_silence=False)
    receiver._relay, receiver._source_ip = RELAY, "192.168.1.5"
    receiver._allowed = {RELAY[0]}
    receiver._identity = b"A_owner".ljust(32, b"\0")
    for index in range(2):
        port = _Port(receiver, index)
        port.transport = MagicMock()
        port.transport.is_closing.return_value = False
        port.number = 6000 + index
        receiver._ports.append(port)
    handshake = asyncio.create_task(receiver._handshake())
    await asyncio.sleep(0)
    registration = sent(receiver._ports[0])[0][0]
    assert registration[20:25] == b"\x01\x00\x00\x00A"
    assert registration[-64:] == b"A_owner".ljust(32, b"\0") * 2
    # The relay's answer is acknowledged with the same kind.
    receiver._setup(receiver._ports[1], candidates(CAMERA), RELAY)
    assert sent(receiver._ports[1])[-1][0][20:23] == b"\x01\x01A"
    receiver._ready.set()
    await handshake
    await receiver.close()


async def test_audio_registration_names_the_audio_identity(socket_enabled, monkeypatch):
    monkeypatch.setattr(tend_rtp, "CONNECT_TIMEOUT", 0.3)
    received = []

    class Relay(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            received.append(data)

    loop = asyncio.get_running_loop()
    relay, _ = await loop.create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
    receiver = TendRtp(lambda record: None, kind=b"A", watch_silence=False)
    try:
        with pytest.raises(CannotConnect):
            await receiver.start(
                ("127.0.0.1", relay.get_extra_info("sockname")[1]), "owner", "", b""
            )
    finally:
        relay.close()
    assert received and all(data[24:25] == b"A" for data in received)
    assert received[0][-64:] == b"A_owner".ljust(32, b"\0") * 2


def test_send_splits_one_rtp_packet_into_numbered_fragments(monkeypatch):
    receiver, _, _ = prepared_receiver(confirmed=True)
    port = receiver._ports[0]
    port.peer = CAMERA
    clock = [100.0]
    monkeypatch.setattr(tend_rtp.time, "monotonic", lambda: clock[0])
    receiver._send_sequence = 65535
    payload = bytes(range(256)) * 5  # 1,280 bytes: three datagrams with the RTP header.
    receiver.send(payload)
    clock[0] += 0.02
    receiver.send(b"next")
    datagrams = sent(port)
    assert [peer for _, peer in datagrams] == [CAMERA] * 4
    headers = [struct.unpack("<IHH", data[:8]) for data, _ in datagrams]
    assert headers == [(0, 3, 0), (0, 3, 1), (0, 3, 2), (1, 1, 0)]
    assert all(len(data) - 8 <= 540 for data, _ in datagrams)
    packet = b"".join(data[8:] for data, _ in datagrams[:3])
    version, kind, sequence, timestamp, source = struct.unpack("!BBHII", packet[:12])
    assert (version, kind, sequence, timestamp, source) == (0x80, 8, 65535, 0, receiver._ssrc)
    assert packet[12:] == payload
    second = datagrams[3][0][8:]
    # The sequence wraps; the timestamp counts 8 kHz from the first packet; no marker.
    assert struct.unpack("!BBHI", second[:8]) == (0x80, 8, 0, 160)
    assert second[12:] == b"next"


def test_send_needs_a_confirmed_media_port():
    receiver, _, _ = prepared_receiver(confirmed=False)
    with pytest.raises(CannotConnect):
        receiver.send(b"audio")
    receiver._ports[0].ready, receiver._ports[0].peer = True, CAMERA
    with pytest.raises(ProtocolError):
        receiver.send(b"audio", payload_type=200)


async def test_audio_connection_does_not_end_on_silence(monkeypatch):
    receiver, _, ended = prepared_receiver(confirmed=True)
    receiver._watch_silence = False
    monkeypatch.setattr(tend_rtp, "FRAME_TIMEOUT", 0)
    original = asyncio.sleep
    calls = []

    async def sleep(_delay):
        calls.append(1)
        if len(calls) > 3:
            receiver._ended.set()
        await original(0)

    monkeypatch.setattr(tend_rtp.asyncio, "sleep", sleep)
    receiver._last_video_at = -1.0
    await receiver._maintain()
    assert ended == [] and len(calls) == 4

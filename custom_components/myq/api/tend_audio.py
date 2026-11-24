"""Talk through a TC camera's speaker, and play the sounds stored on a camera.

Talkback goes over the camera's audio connection on its Tend signaling
(AudioConnectionManager, see tend_legacy.AudioLink): "SDNK" messages, START, DATA
every 20 ms and STOP, carried as the RTP payload (or written raw to the TLS
channel). Audio is Opus at 16 kHz or G.711 µ-law at 8 kHz, as the camera's codec list
allows. No alias, address, key or session identifier is logged.
"""

from __future__ import annotations

import asyncio
import io
import re
import struct
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import av

from .errors import CannotConnect, CommandRejected, MyQError
from .live_audio import CODEC_NAMES
from .models import Device, nested

if TYPE_CHECKING:
    from .client import Client
    from .tend_legacy import AudioLink, LegacyTendSession

VIDEO = "https://media.hubs.tend-us.tendplatform.com"
FRAME_MS = 20
MAX_SECONDS = 120  # The longest clip sent in one talk.
# How long a talk waits for the camera's own sound to learn its codec, when the camera
# lists none.
HEAR_SECONDS = 3.0
_START, _STOP, _DATA = 7, 8, 9
# Demuxers FFmpeg may use on downloaded audio. None of them opens other inputs, so a
# playlist (HLS, concat) can never make it fetch anything.
_FORMATS = "mp3,wav,ogg,flac,mov,aac,matroska,adts,au,aiff,amr"


def pick_codec(names: Iterable[str]) -> str | None:
    """AudioConnectionManager.talkbackCodec(): Opus when listed, else G.711 µ-law."""
    listed = {name.strip().lower() for name in names}
    if "opus" in listed:
        return "opus"
    if any(name.startswith("g711") for name in listed):
        return "g711"
    return None


def talkback_codec(details: dict) -> str | None:
    """The talkback codec the camera's details list (cap.spk.codecList), if any."""
    codecs = nested(details, "cap", "spk", "codecList")
    if not isinstance(codecs, list):
        return None
    return pick_codec(item for item in codecs if isinstance(item, str))


def can_talk(details: dict) -> bool:
    """cap.spk.talkback. Without a codec list in the details, the codec comes from the
    camera's row in the Tend login answer (SPKCDLIST), once connected."""
    return nested(details, "cap", "spk", "talkback") is True


def stored_tracks(details: dict) -> list[tuple[str, str]]:
    """(track id, description) of the sounds a camera can play (cap.spk.playbackTrackList)."""
    if nested(details, "cap", "spk", "playback") is not True:
        return []
    tracks = nested(details, "cap", "spk", "playbackTrackList")
    found = []
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        track_id, description = track.get("id"), track.get("descr")
        if isinstance(track_id, str) and 0 < len(track_id) <= 128 and track_id.isprintable():
            label = description if isinstance(description, str) and description.strip() else None
            found.append((track_id, (label or track_id)[:128]))
    return found


def speaker_summary(details: dict) -> dict[str, Any]:
    """The speaker capabilities that decide talkback and stored sounds; no identifiers."""

    def flag(name: str) -> bool | None:
        value = nested(details, "cap", "spk", name)
        return value if type(value) is bool else None

    codecs = nested(details, "cap", "spk", "codecList")
    listed = (
        sorted(
            {
                item.strip().lower()
                for item in codecs
                if isinstance(item, str) and re.fullmatch(r"\s*[A-Za-z0-9_.-]{1,24}\s*", item)
            }
        )[:8]
        if isinstance(codecs, list)
        else None
    )
    return {
        "talkback": flag("talkback"),
        "full_duplex": flag("fullDuplex"),
        "codecs": listed,
        "talkback_codec": talkback_codec(details),
        "playback": flag("playback"),
        "stored_sounds": len(stored_tracks(details)),
    }


async def play_track(client: Client, device: Device, track_id: str) -> None:
    """POST /cxs/api/devices/{id}/audio/player/do/play (Devices.playAudioTrack)."""
    if not device.is_camera or not device.camera_id:
        raise CommandRejected("This device is not a camera")
    await client.device_mutation(
        device,
        "POST",
        VIDEO,
        "/cxs/api/devices/" + quote(device.camera_id, safe="") + "/audio/player/do/play",
        headers={"AntiCSRF": "AntiCSRF"},
        json={"trackId": track_id},
    )


def _message(kind: int, dest: bytes, sid: bytes, tail: bytes) -> bytes:
    body = bytes([len(sid)]) + sid + tail
    return (
        b"SDNK"
        + bytes([1, kind, 1, 0, 0])
        + struct.pack("<H", len(dest))
        + dest
        + struct.pack("<I", len(body))
        + body
    )


def start_message(dest: bytes, sid: bytes, codec: str) -> bytes:
    """START: the codec (16 for Opus, 1 otherwise) and seven zero bytes."""
    return _message(_START, dest, sid, bytes([16 if codec == "opus" else 1]) + bytes(7))


def data_message(dest: bytes, sid: bytes, sequence: int, audio: bytes) -> bytes:
    """DATA: a sequence number from 0, the audio's length, the audio and four zero bytes."""
    return _message(
        _DATA, dest, sid, struct.pack("<II", sequence & 0xFFFFFFFF, len(audio)) + audio + bytes(4)
    )


def stop_message(dest: bytes, sid: bytes) -> bytes:
    return _message(_STOP, dest, sid, bytes(4))


def encode_audio(data: bytes, codec: str, max_seconds: float = MAX_SECONDS) -> list[bytes]:
    """Decode an audio file and encode it in 20 ms frames for the camera (blocking).

    Opus: libopus at 16 kHz mono, VOIP, complexity 1, constant bit rate at libopus' own
    default (InitOpusAudio). G.711: µ-law at 8 kHz.
    """
    if codec not in ("opus", "g711"):
        raise CommandRejected("The camera does not accept a supported talkback codec")
    rate = 16000 if codec == "opus" else 8000
    samples = rate * FRAME_MS // 1000
    limit = int(max_seconds * rate) * 2
    pcm = bytearray()
    try:
        with av.open(
            io.BytesIO(data), mode="r", options={"format_whitelist": _FORMATS}
        ) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise CommandRejected("The media has no audio")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):  # type: ignore[arg-type]
                    pcm += bytes(out.planes[0])[: out.samples * 2]
                if len(pcm) >= limit:
                    break
            else:
                for out in resampler.resample(None):
                    pcm += bytes(out.planes[0])[: out.samples * 2]
    except (av.FFmpegError, ValueError, UnicodeError):
        raise CommandRejected("The media could not be read as audio") from None
    del pcm[limit:]
    if not pcm:
        raise CommandRejected("The media has no audio")
    step = samples * 2
    if len(pcm) % step:
        pcm += bytes(step - len(pcm) % step)
    try:
        if codec == "opus":
            context = av.CodecContext.create("libopus", "w")
            context.options = {
                "application": "voip",
                "vbr": "off",
                "compression_level": "1",
                "frame_duration": str(FRAME_MS),
            }
            # libopus' own default for 16 kHz mono 20 ms frames: 60*Fs/frame + Fs*channels.
            context.bit_rate = 60 * rate // samples + rate
        else:
            context = av.CodecContext.create("pcm_mulaw", "w")
    except (av.FFmpegError, ValueError):
        raise CommandRejected("This Home Assistant cannot encode talkback audio") from None
    if not isinstance(context, av.AudioCodecContext):
        raise CommandRejected("This Home Assistant cannot encode talkback audio")
    context.sample_rate, context.layout, context.format = rate, "mono", "s16"
    packets: list[bytes] = []
    for index, offset in enumerate(range(0, len(pcm), step)):
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(bytes(pcm[offset : offset + step]))
        frame.sample_rate, frame.pts = rate, index * samples
        packets.extend(bytes(packet) for packet in context.encode(frame))
    packets.extend(bytes(packet) for packet in context.encode(None))
    return [packet for packet in packets if packet]


class Talkback:
    """One talk over the camera's audio connection: START, the audio, then STOP.

    A live view's audio connection is shared; otherwise the talk opens one of its own
    and closes it afterwards.
    """

    def __init__(self, session: LegacyTendSession) -> None:
        self._session = session
        self._link: AudioLink | None = None
        self._owned = False
        self._sid = str(int(time.time() * 1000)).encode("ascii")
        self._dest = session._device_alias.encode("utf-8")

    async def open(self) -> None:
        """Take the session's audio connection, or open one for this talk."""
        self._link, self._owned = await self._session.talk_link()

    async def heard_codec(self, seconds: float = HEAR_SECONDS) -> str | None:
        """The codec the camera's own sound arrives in on the connection, if any.

        The app talks back in it when the camera lists no talkback codec
        (AudioConnectionManager.talkbackCodec, getCodec).
        """
        if self._link is None:
            return None
        return CODEC_NAMES.get(await self._link.heard(seconds) or -1)

    async def _send(self, message: bytes) -> None:
        if self._link is None:
            raise CannotConnect("Talkback is not open")
        await self._link.send(message)

    async def stream(
        self,
        frames: list[bytes],
        codec: str,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        """START, a DATA message every 20 ms in real time, then STOP (even if cancelled)."""
        loop = asyncio.get_running_loop()
        try:
            await self._send(start_message(self._dest, self._sid, codec))
            await sleep(FRAME_MS / 1000)
            began = loop.time()
            for sequence, frame in enumerate(frames):
                await self._send(data_message(self._dest, self._sid, sequence, frame))
                await sleep(max(0.0, began + (sequence + 1) * FRAME_MS / 1000 - loop.time()))
        finally:
            try:
                async with asyncio.timeout(2):
                    await self._send(stop_message(self._dest, self._sid))
            except (OSError, TimeoutError, MyQError):
                pass

    async def close(self) -> None:
        """Close a connection opened for this talk; a shared one stays open."""
        link, self._link = self._link, None
        if link is not None and self._owned:
            await link.close()

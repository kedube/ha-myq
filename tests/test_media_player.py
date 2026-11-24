"""The camera speaker entity: talkback through Home Assistant, with synthetic sessions."""

import asyncio
import io
import math
import struct
import wave
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.media_player import BrowseMedia, MediaPlayerEntityFeature
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.errors import CannotConnect
from custom_components.myq.const import DOMAIN

URL = "http://media.example.invalid/hello.wav"
OWNER = AccountAccess(
    "test-account", "owner", {key: frozenset({"All"}) for key in ("devices", "alerts")}
)


def wav(seconds=0.1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(int(seconds * 16000))
            )
        )
    return buffer.getvalue()


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, openers=()):
        self.openers = set(openers)
        self.connect = AsyncMock()
        self.close = AsyncMock()
        self.connected = True
        FakeSession.instances.append(self)


class FakeTalk:
    instances: list["FakeTalk"] = []
    open_error: Exception | None = None
    heard: str | None = None  # The codec of the camera's own sound on the connection.

    def __init__(self, session):
        self.session, self.frames, self.codec = session, None, None
        self.release = asyncio.Event()
        self.streaming = asyncio.Event()
        self.closed = self.cancelled = False
        FakeTalk.instances.append(self)

    async def open(self):
        if FakeTalk.open_error is not None:
            raise FakeTalk.open_error

    async def heard_codec(self):
        return FakeTalk.heard

    async def stream(self, frames, codec):
        self.frames, self.codec = frames, codec
        self.streaming.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        self.closed = True


def speaker_caps(details, *, talk=True, tracks=True):
    spk = details["cap"]["spk"]
    if talk:
        spk.update({"talkback": True, "codecList": ["opus", "g711"]})
    if tracks:
        spk.update({"playback": True, "playbackTrackList": [{"id": "t1", "descr": "Chime"}]})


@pytest.fixture
async def speaker(
    hass, aioclient_mock, enable_custom_integrations, tokens, camera_device, camera_details
):
    camera_device = replace(
        camera_device,
        serial="TC-TEST-CAMERA",
        state={"online": True, "paired_gdo_serial_number": "OPENER-1"},
    )
    camera_details["alias"] = "owner:camera"
    speaker_caps(camera_details)
    FakeSession.instances, FakeTalk.instances, FakeTalk.open_error = [], [], None
    FakeTalk.heard = None
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-speaker-user",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    mutation = AsyncMock()
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
            "custom_components.myq.api.management.Management.account_access",
            AsyncMock(return_value={"test-account": OWNER}),
        ),
        patch("custom_components.myq.api.client.Client.device_mutation", mutation),
        patch("custom_components.myq.media_player.LegacyTendSession", FakeSession),
        patch("custom_components.myq.media_player.Talkback", FakeTalk),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        states = hass.states.async_all("media_player")
        entity_id = states[0].entity_id if states else None
        yield SimpleNamespace(
            entry=entry,
            entity_id=entity_id,
            coordinator=entry.runtime_data,
            device=camera_device,
            details=camera_details,
            mutation=mutation,
        )
        for talk in FakeTalk.instances:
            talk.release.set()
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def play(hass, entity_id, media_id):
    await hass.services.async_call(
        "media_player",
        "play_media",
        {"entity_id": entity_id, "media_content_id": media_id, "media_content_type": "music"},
        blocking=True,
    )


async def test_tc_camera_with_talkback_gets_a_speaker(hass, speaker):
    state = hass.states.get(speaker.entity_id)
    assert state.state == "idle"
    features = MediaPlayerEntityFeature(state.attributes["supported_features"])
    assert features == (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
        | MediaPlayerEntityFeature.STOP
    )


async def test_audio_is_downloaded_encoded_and_sent_over_its_own_session(
    hass, speaker, aioclient_mock
):
    aioclient_mock.get(URL, content=wav())
    coordinator = speaker.coordinator
    await play(hass, speaker.entity_id, URL)
    talk = FakeTalk.instances[0]
    await talk.streaming.wait()
    session = FakeSession.instances[0]
    assert talk.session is session and talk.codec == "opus" and len(talk.frames) >= 5
    assert session.openers == {"OPENER-1"}
    session.connect.assert_awaited_once_with("owner:camera", "test-access")
    assert session in coordinator._live_owners
    assert hass.states.get(speaker.entity_id).state == "playing"
    talk.release.set()
    await hass.async_block_till_done()
    assert talk.closed and not talk.cancelled
    session.close.assert_awaited_once()
    assert session not in coordinator._live_owners
    assert hass.states.get(speaker.entity_id).state == "idle"


async def test_stop_and_new_media_end_the_talk_in_progress(hass, speaker, aioclient_mock):
    aioclient_mock.get(URL, content=wav())
    await play(hass, speaker.entity_id, URL)
    first = FakeTalk.instances[0]
    await first.streaming.wait()
    # Something new replaces what is playing.
    await play(hass, speaker.entity_id, URL)
    assert first.cancelled and first.closed and len(FakeTalk.instances) == 2
    await hass.services.async_call(
        "media_player", "media_stop", {"entity_id": speaker.entity_id}, blocking=True
    )
    second = FakeTalk.instances[1]
    assert second.cancelled and second.closed
    assert all(session.close.await_count == 1 for session in FakeSession.instances)
    assert hass.states.get(speaker.entity_id).state == "idle"


async def test_talk_uses_the_open_live_video_session(hass, speaker, aioclient_mock):
    aioclient_mock.get(URL, content=wav())
    shared = FakeSession()
    source = speaker.coordinator.video_sources[speaker.device.key]
    source._session = shared
    try:
        await play(hass, speaker.entity_id, URL)
        talk = FakeTalk.instances[0]
        await talk.streaming.wait()
        assert talk.session is shared and len(FakeSession.instances) == 1
        talk.release.set()
        await hass.async_block_till_done()
        # The live view's session stays open.
        shared.close.assert_not_awaited()
    finally:
        source._session = None


async def test_failure_to_open_reaches_the_caller(hass, speaker, aioclient_mock):
    aioclient_mock.get(URL, content=wav())
    FakeTalk.open_error = CannotConnect("The camera did not answer the talkback request")
    with pytest.raises(HomeAssistantError):
        await play(hass, speaker.entity_id, URL)
    await hass.async_block_till_done()
    assert FakeTalk.instances[0].closed
    session = FakeSession.instances[0]
    session.close.assert_awaited_once()
    assert session not in speaker.coordinator._live_owners
    assert hass.states.get(speaker.entity_id).state == "idle"


async def test_bad_downloads_are_refused_before_any_session(hass, speaker, aioclient_mock):
    aioclient_mock.get(URL, status=404)
    with pytest.raises(HomeAssistantError):
        await play(hass, speaker.entity_id, URL)
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, content=b"not audio")
    with pytest.raises(HomeAssistantError):
        await play(hass, speaker.entity_id, URL)
    with pytest.raises(HomeAssistantError):
        await play(hass, speaker.entity_id, "ftp://media.example.invalid/hello.wav")
    assert not FakeSession.instances


async def test_stored_sound_is_played_by_track_id(hass, speaker):
    await play(hass, speaker.entity_id, "myq_track:t1")
    call = speaker.mutation.await_args
    assert call.args[1:] == (
        "POST",
        "https://media.hubs.tend-us.tendplatform.com",
        f"/cxs/api/devices/{speaker.device.camera_id}/audio/player/do/play",
    )
    assert call.kwargs["json"] == {"trackId": "t1"}
    with pytest.raises(HomeAssistantError):
        await play(hass, speaker.entity_id, "myq_track:unknown")
    assert not FakeSession.instances


async def test_browsing_lists_stored_sounds_and_media_sources(hass, speaker):
    entity = hass.data["entity_components"]["media_player"].get_entity(speaker.entity_id)
    sources = BrowseMedia(
        media_class="directory",
        media_content_id="media-source://",
        media_content_type="",
        title="Media",
        can_play=False,
        can_expand=True,
    )
    with patch(
        "custom_components.myq.media_player.media_source.async_browse_media",
        AsyncMock(return_value=sources),
    ):
        root = await entity.async_browse_media()
    assert [child.media_content_id for child in root.children] == [
        "myq_track:t1",
        "media-source://",
    ]
    assert root.children[0].title == "Chime" and root.children[0].can_play


async def test_speaker_needs_control_permission(hass, speaker):
    speaker.coordinator.data.account_access.clear()
    speaker.coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(speaker.entity_id).state == "unavailable"


@pytest.mark.parametrize(
    ("serial", "talk", "tracks", "expected"),
    [
        ("TC-TEST-CAMERA", False, False, None),
        # A WebRTC camera talks through its live view; only its stored sounds play here.
        ("TEST-CAM123", True, True, MediaPlayerEntityFeature.PLAY_MEDIA),
        ("TEST-CAM123", True, False, None),
    ],
)
async def test_which_cameras_get_a_speaker(
    hass,
    enable_custom_integrations,
    tokens,
    camera_device,
    camera_details,
    serial,
    talk,
    tracks,
    expected,
):
    device = replace(camera_device, serial=serial)
    camera_details["alias"] = "owner:camera"
    camera_details["cap"]["cam"]["kvsWebRTC"] = True
    speaker_caps(camera_details, talk=talk, tracks=tracks)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-speaker-choice",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={device.key: device}),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(return_value=camera_details),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        states = hass.states.async_all("media_player")
        if expected is None:
            assert states == []
        else:
            features = MediaPlayerEntityFeature(states[0].attributes["supported_features"])
            assert features == expected | MediaPlayerEntityFeature.BROWSE_MEDIA
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    "listed,heard,codec,encodes",
    [
        (["g711"], None, "g711", 2),  # Encoded as Opus first, then for the camera's codec.
        (["opus"], "g711", "opus", 1),  # A listed codec comes first.
        # Nothing listed: the codec of the camera's own sound, as the app talks back.
        ([], "g711", "g711", 2),
        ([], "opus", "opus", 1),
        ([], None, None, 1),
    ],
)
async def test_camera_without_a_codec_list_talks_in_its_login_rows_codec(
    hass, speaker, aioclient_mock, monkeypatch, listed, heard, codec, encodes
):
    # Cameras built into openers list no codecs in their details (cap.spk.codecList).
    speaker.details["cap"]["spk"].pop("codecList")
    aioclient_mock.get(URL, content=wav())
    from custom_components.myq import media_player

    calls = []
    real = media_player.encode_audio

    def encode(data, name):
        calls.append(name)
        return real(data, name)

    monkeypatch.setattr(media_player, "encode_audio", encode)
    original = FakeSession.__init__

    def with_codecs(self, openers=()):
        original(self, openers)
        self.speaker_codecs = listed

    monkeypatch.setattr(FakeSession, "__init__", with_codecs)
    FakeTalk.heard = heard
    if codec is None:
        with pytest.raises(HomeAssistantError, match="talkback codec"):
            await play(hass, speaker.entity_id, URL)
        [talk] = FakeTalk.instances
        assert talk.closed and talk.frames is None  # Opened, nothing sent, closed.
    else:
        await play(hass, speaker.entity_id, URL)
        talk = FakeTalk.instances[0]
        await talk.streaming.wait()
        assert talk.codec == codec
        talk.release.set()
        await hass.async_block_till_done()
    assert len(calls) == encodes and calls[0] == "opus"
    session = FakeSession.instances[0]
    session.close.assert_awaited_once()

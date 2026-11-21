"""Siren and saved PTZ positions without a live view; camera/opener links; door clips."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api import camera_commands
from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.camera_commands import CameraCommands, presets
from custom_components.myq.api.errors import CommandRejected
from custom_components.myq.api.history import HistoryEvent
from custom_components.myq.api.live import LiveSession
from custom_components.myq.api.models import Device
from custom_components.myq.api.recordings import Recording
from custom_components.myq.const import DOMAIN

OWNER = AccountAccess("test-account", "owner", {"devices": frozenset({"All"})})


def details(**extra):
    return {
        "alias": "camera-alias",
        "status": {"online": True, "cam": {"ptz": {"calibrationResult": "success"}}},
        "cap": {"cam": {"ptz": {}}, "siren": {"activatable": True}},
        "settings": {
            "cam": {
                "enabled": True,
                "ptz": {"presets": [{"id": 2, "name": "Driveway"}, {"id": 5}, {"id": "x"}]},
            }
        },
        **extra,
    }


def test_presets_follow_preset_dto():
    assert presets(details()) == [(2, "Driveway"), (5, "Preset 5")]
    drifted = details()
    drifted["settings"]["cam"]["ptz"]["presetDrifted"] = True
    assert presets(drifted) == []


async def test_commands_prefer_live_view_then_status_session(camera_device):
    stream = SimpleNamespace(ready=True, send_command=AsyncMock())
    monitor = SimpleNamespace(command=AsyncMock(return_value=True))
    commands = CameraCommands(MagicMock(), monitor, lambda key: stream)
    await commands.go_to_preset(camera_device, details(), 2)
    stream.send_command.assert_awaited_once_with("set-ptz", (("G", "2"),))
    monitor.command.assert_not_awaited()
    commands = CameraCommands(MagicMock(), monitor, lambda key: None)
    await commands.go_to_preset(camera_device, details(), 5)
    monitor.command.assert_awaited_once_with("camera-alias", "set-ptz", (("G", "5"),))
    with pytest.raises(CommandRejected):
        await commands.go_to_preset(camera_device, details(), 9)


async def test_one_off_login_when_no_session_is_open(camera_device):
    client = MagicMock()
    client.auth.access_token = AsyncMock(return_value="token")
    monitor = SimpleNamespace(command=AsyncMock(return_value=False))
    session = MagicMock(open=AsyncMock(), send_to=AsyncMock(), close=AsyncMock())
    with patch.object(camera_commands, "TendCommandSession", return_value=session):
        commands = CameraCommands(client, monitor, lambda key: None)
        await commands.stop_siren(camera_device, details())
    session.open.assert_awaited_once_with("token")
    session.send_to.assert_awaited_once_with(
        "camera-alias", "trigger-siren-action", (("31", "02"),)
    )
    session.close.assert_awaited_once()


async def test_timed_siren_always_ends_with_a_stop(camera_device, monkeypatch):
    sent = []
    monitor = SimpleNamespace(
        command=AsyncMock(side_effect=lambda alias, cmd, actions: sent.append(actions) or True)
    )
    commands = CameraCommands(MagicMock(), monitor, lambda key: None)
    await commands.siren(camera_device, details(), seconds=1)
    assert sent == [(("31", "01"),)]
    # Stopping early cancels the timer, whose cleanup sends the stop once.
    await commands.stop_siren(camera_device, details())
    assert sent == [(("31", "01"),), (("31", "02"),)]
    await commands.siren(camera_device, details(), seconds=1)
    await commands.close()
    assert sent[-1] == (("31", "02"),)
    no_siren = details()
    no_siren["cap"]["siren"]["activatable"] = False
    with pytest.raises(CommandRejected):
        await commands.siren(camera_device, no_siren)


async def test_live_signaling_errors_are_ignored_once_media_flows():
    stream = LiveSession(None, AsyncMock())
    stream._connected = stream._ready = True
    await stream._signaling_error(CommandRejected("GO_AWAY"))
    await asyncio.sleep(0)
    assert not stream._failed
    stream._connected = False
    await stream._signaling_error(CommandRejected("GO_AWAY"))
    await asyncio.sleep(0.01)
    assert stream._failed


async def test_join_requires_a_ready_session():
    stream = LiveSession(None, AsyncMock())
    with pytest.raises(CommandRejected, match="not ready"):
        await stream.join("v=0", "second")


def test_live_frame_encodes_as_jpeg():
    import av

    from custom_components.myq.api.live import encode_jpeg

    frame = av.VideoFrame(width=32, height=24, format="yuv420p")
    assert encode_jpeg(frame).startswith(b"\xff\xd8\xff")


# --- HA: buttons without a view, opener links, door clips ------------------------------


def opener(camera_serial):
    return Device.parse(
        "test-account",
        {
            "serial_number": "GDO1",
            "name": "Garage",
            "device_type": "wifigaragedooropener",
            "device_family": "garagedoor",
            "state": {
                "online": True,
                "door_state": "closed",
                "has_camera": True,
                "attached_camera_serial_number": camera_serial,
            },
        },
    )


@pytest.fixture
async def loaded(hass, enable_custom_integrations, tokens, camera_device):
    door = opener(camera_device.serial)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value={door.key: door, camera_device.key: camera_device}),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(return_value=details()),
        ),
        patch(
            "custom_components.myq.api.management.Management.account_access",
            AsyncMock(return_value={"test-account": OWNER}),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield SimpleNamespace(entry=entry, door=door, coordinator=entry.runtime_data)


async def test_camera_device_is_linked_to_its_opener(hass, loaded, camera_device):
    registry = hass.data["device_registry"]
    camera = registry.async_get_device(identifiers={(DOMAIN, camera_device.key)})
    garage = registry.async_get_device(identifiers={(DOMAIN, loaded.door.key)})
    assert camera.via_device_id == garage.id


async def test_siren_and_preset_buttons_work_without_a_live_view(hass, loaded):
    siren = hass.states.get("button.test_camera_sound_siren_for_10_seconds")
    preset = hass.states.get("button.test_camera_go_to_driveway")
    assert siren.state != "unavailable" and preset.state != "unavailable"
    commands = loaded.coordinator.camera_commands
    with (
        patch.object(commands, "siren", AsyncMock()) as sound,
        patch.object(commands, "go_to_preset", AsyncMock()) as move,
        patch.object(loaded.coordinator.video, "details", AsyncMock(return_value=details())),
        patch.object(
            loaded.coordinator.management,
            "account_access",
            AsyncMock(return_value={"test-account": OWNER}),
        ),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": siren.entity_id}, blocking=True
        )
        await hass.services.async_call(
            "button", "press", {"entity_id": preset.entity_id}, blocking=True
        )
    sound.assert_awaited_once()
    assert move.call_args.args[2] == 2


async def test_door_history_links_the_opener_camera_clip(hass, loaded, camera_device):
    coordinator = loaded.coordinator
    moment = datetime(2026, 11, 20, 8, 0, 5, 123000, tzinfo=UTC)
    event = HistoryEvent("h1", moment, "device_action", "open", loaded.door.serial)
    other = HistoryEvent("h2", moment.replace(hour=9), "device_action", "closed")
    clip = Recording(
        "clip-1",
        camera_device.camera_id,
        "2026-11-20T08:00:06.000Z",
        "VIDEO",
        door="open",
        door_time="2026-11-20T08:00:05.123Z",
    )
    with patch.object(coordinator.recordings, "door_clips", AsyncMock(return_value=(clip,))):
        links = await coordinator.door_clip_links(loaded.door, [event, other])
    assert list(links) == ["h1"]
    assert links["h1"].startswith("media-source://myq/")
    assert links["h1"].endswith("/event/clip-1")

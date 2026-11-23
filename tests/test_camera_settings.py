"""Explicit Tend settings: native payloads, capabilities, and entity readback."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.myq.api.camera_settings import (
    CameraSettings,
    choices,
    lights,
    supported,
)
from custom_components.myq.api.client import Client
from custom_components.myq.api.errors import (
    CannotConnect,
    CommandRejected,
    CommandUncertain,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.video import VIDEO, Video
from custom_components.myq.camera_entities import camera_entities
from tests.conftest import json_response


@pytest.fixture
def details(camera_details):
    result = copy.deepcopy(camera_details)
    result["cap"]["cam"].update(
        {
            "motionSensMin": 1,
            "motionSensMax": 10,
            "ir": True,
            "rotationList": [1, 3],
            "ptz": {"autoTracking": {"enabled": True}},
        }
    )
    result["settings"]["cam"].update(
        {
            "motionSens": 5,
            "ir": 2,
            "rotation": 1,
            "res": "FHD",
            "fps": 15,
            "quality": 5,
            "ptz": {"autoTracking": {"enabled": False, "objects": ["person"]}},
        }
    )
    result["cap"].update(
        {
            "statusLed": {"ledOnOff": True},
            "siren": {"activatable": True, "minVol": 0, "maxVol": 100},
            "chime": {"typeList": ["D", "M", "N"]},
            "lightList": [
                {
                    "switchStateList": ["on", "off", "program"],
                    "setIntensity": True,
                    "intensityMin": 10,
                    "intensityMax": 100,
                }
            ]
            * 2,
        }
    )
    result["settings"].update(
        {
            "statusLed": {"status": "ON"},
            "siren": {"vol": 80},
            "chime": {"enabled": True, "type": "D", "duration": 8},
            "lightList": [
                {
                    "switchState": "off",
                    "intensity": 50,
                    "triggerDuration": "PT1M",
                    "motionTrigger": True,
                },
                {
                    "switchState": "program",
                    "intensity": 25,
                    "triggerDuration": "PT2M",
                    "motionTrigger": False,
                },
            ],
        }
    )
    return result


@pytest.mark.parametrize(
    "key,desired,payload",
    [
        ("motion_sensitivity", 8, {"cam": {"motionSens": 8}}),
        ("siren_volume", 70, {"siren": {"vol": 70}}),
        ("rotation", "mirror_flip", {"cam": {"rotation": 3}}),
        ("night_vision", "off", {"cam": {"ir": 0}}),
        ("status_led", False, {"statusLed": {"status": "OFF"}}),
        ("chime_enabled", False, {"chime": {"enabled": False, "type": "D", "duration": 8}}),
        (
            "auto_tracking",
            True,
            {"cam": {"ptz": {"autoTracking": {"enabled": True, "objects": ["person"]}}}},
        ),
    ],
)
async def test_native_setting_payloads_and_headers(
    http, tokens, camera_device, details, key, desired, payload
):
    http.request.side_effect = [json_response(details), Response(204, {}, b"")]
    api = CameraSettings(Video(Client(transport=http, tokens=tokens)))
    await api.set(camera_device, key, desired)
    assert http.request.await_count == 2
    http.request.assert_awaited_with(
        "PUT",
        VIDEO + "/cxs/api/devices/CAM123/settings",
        headers={"AntiCSRF": "AntiCSRF", "Authorization": "Bearer test-access"},
        json=payload,
    )


@pytest.mark.parametrize(
    "key,desired",
    [
        ("motion_sensitivity", True),
        ("motion_sensitivity", 11),
        ("siren_volume", 0),
        ("siren_volume", 65),
        ("rotation", "mirror"),
        ("night_vision", "guess"),
        ("status_led", 1),
        ("chime_enabled", "false"),
        ("arbitrary_command", True),
    ],
)
async def test_invalid_values_never_write(http, tokens, camera_device, details, key, desired):
    http.request.return_value = json_response(details)
    api = CameraSettings(Video(Client(transport=http, tokens=tokens)))
    with pytest.raises(CommandRejected):
        await api.set(camera_device, key, desired)
    assert http.request.await_count == 1


async def test_capability_rechecked_before_mutation(http, tokens, camera_device, details):
    assert supported(details, "status_led")
    details["cap"]["statusLed"]["ledOnOff"] = False
    http.request.return_value = json_response(details)
    api = CameraSettings(Video(Client(transport=http, tokens=tokens)))
    with pytest.raises(CommandRejected):
        await api.set(camera_device, "status_led", True)
    assert http.request.await_count == 1


@pytest.mark.parametrize("field", ["online", "enabled"])
async def test_tracking_rejects_offline_or_private_camera(
    http, tokens, camera_device, details, field
):
    if field == "online":
        details["status"]["online"] = False
    else:
        details["settings"]["cam"]["enabled"] = False
    http.request.return_value = json_response(details)
    api = CameraSettings(Video(Client(transport=http, tokens=tokens)))
    with pytest.raises(CommandRejected):
        await api.set(camera_device, "auto_tracking", True)
    assert http.request.await_count == 1


async def test_light_array_preserves_other_light_and_native_fields(
    http, tokens, camera_device, details
):
    original = copy.deepcopy(details)
    http.request.side_effect = [json_response(details), Response(204, {}, b"")]
    api = CameraSettings(Video(Client(transport=http, tokens=tokens)))
    await api.light(camera_device, 0, True, 255)
    body = http.request.await_args.kwargs["json"]
    expected = original["settings"]["lightList"]
    expected[0].update({"switchState": "on", "intensity": 100})
    assert body == {"lightList": expected}
    assert details["settings"]["lightList"][0]["switchState"] == "off"


@pytest.mark.parametrize("index,brightness", [(2, None), (True, None), (0, True), (0, 256)])
async def test_invalid_light_commands_never_write(
    http, tokens, camera_device, details, index, brightness
):
    http.request.return_value = json_response(details)
    with pytest.raises(CommandRejected):
        await CameraSettings(Video(Client(transport=http, tokens=tokens))).light(
            camera_device, index, True, brightness
        )
    assert http.request.await_count == 1


async def test_light_brightness_requires_advertised_support(http, tokens, camera_device, details):
    details["cap"]["lightList"][0]["setIntensity"] = False
    http.request.return_value = json_response(details)
    with pytest.raises(CommandRejected):
        await CameraSettings(Video(Client(transport=http, tokens=tokens))).light(
            camera_device, 0, True, 128
        )
    assert http.request.await_count == 1


async def test_ambiguous_settings_write_is_not_replayed(http, tokens, camera_device, details):
    http.request.side_effect = [json_response(details), CannotConnect("uncertain")]
    with pytest.raises(CommandUncertain):
        await CameraSettings(Video(Client(transport=http, tokens=tokens))).set(
            camera_device, "status_led", False
        )
    assert http.request.await_count == 2


@pytest.mark.parametrize("malformed", [None, [], "true", 1])
def test_invalid_capability_shapes_grant_no_control(details, malformed):
    details["cap"]["cam"] = malformed
    for key in ("motion_sensitivity", "night_vision", "rotation", "auto_tracking"):
        assert not supported(details, key)
    details["cap"]["lightList"] = malformed
    assert lights(details) == []


def test_explicit_enum_dto_shape_is_preserved(details):
    details["settings"]["cam"].update({"rotation": "NORMAL", "ir": "AUTO"})
    assert choices(details, "rotation") == {"normal": "NORMAL", "mirror_flip": "MIRROR_FLIP"}
    assert choices(details, "night_vision") == {"off": "OFF", "on": "ON", "auto": "AUTO"}


async def test_removed_device_rejects_before_read(http, tokens):
    with pytest.raises(CommandRejected):
        await CameraSettings(Video(Client(transport=http, tokens=tokens))).set(
            None, "status_led", True
        )
    http.request.assert_not_awaited()


def test_entity_factories_and_current_readback(hass, camera_device, details):
    coordinator = SimpleNamespace(
        hass=hass,
        config_entry=None,
        data=SimpleNamespace(cameras={camera_device.key: details}),
        async_add_listener=lambda *args: None,
        last_update_success=True,
    )
    coordinator.data.devices = {camera_device.key: camera_device}
    # select: rotation, night vision and chime type.
    expected_counts = {"switch": 3, "number": 2, "select": 3, "light": 2, "sensor": 3}
    for platform, count in expected_counts.items():
        assert len(list(camera_entities(coordinator, camera_device, platform))) == count
    first, second = camera_entities(coordinator, camera_device, "light")
    assert first.is_on is False
    assert second.is_on is None  # program is not evidence that the light is currently on.
    assert first.brightness == 114
    details["settings"]["cam"].update({"res": "", "fps": float("inf"), "quality": True})
    assert list(camera_entities(coordinator, camera_device, "sensor")) == []
    details["cap"]["lightList"][0]["setIntensity"] = False
    assert first.brightness is None
    device = replace(camera_device, state={"online": False})
    coordinator.data.devices[device.key] = device
    assert not first.available


def test_unexpected_chime_type_shape_reads_as_no_selection():
    """A list is unhashable: looking it up in the alias table raised TypeError."""
    from custom_components.myq.api.camera_settings import value

    assert value({"settings": {"chime": {"type": ["D"]}}}, "chime_type") is None

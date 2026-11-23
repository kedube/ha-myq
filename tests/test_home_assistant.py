"""Load the integration into real Home Assistant with synthetic cloud responses."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_CLOSED, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.errors import (
    AccessDenied,
    AuthenticationError,
    CannotConnect,
    RequestUnauthorized,
    SessionRenewalFailed,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.models import Device
from custom_components.myq.api.tokens import REVOCATION_URL, Tokens
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import Snapshot
from custom_components.myq.diagnostics import async_get_config_entry_diagnostics

from .conftest import json_response


@pytest.fixture(autouse=True)
def allow_custom(enable_custom_integrations):
    yield


@pytest.fixture
def entry(hass, tokens):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
    )
    entry.add_to_hass(hass)
    return entry


async def setup(hass, entry, door, camera_device=None, camera_details=None):
    devices = {door.key: door}
    if camera_device:
        devices[camera_device.key] = camera_device
    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(return_value=devices),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(return_value=camera_details),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


async def test_load_door_and_camera_and_unload(hass, entry, door, camera_device, camera_details):
    coordinator = await setup(hass, entry, door, camera_device, camera_details)
    assert entry.state == ConfigEntryState.LOADED
    covers = hass.states.async_all("cover")
    assert len(covers) == 1 and covers[0].state == STATE_CLOSED
    cameras = hass.states.async_all("camera")
    assert len(cameras) == 1
    assert cameras[0].attributes["supported_features"] == 1  # ON_OFF; no unverified STREAM flag.
    assert len(hass.states.async_all("switch")) == 2
    assert len(hass.states.async_all("number")) == 1
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diagnostic)
    for secret in (door.serial, door.account_id, door.name, "test-access", "test-refresh"):
        assert secret not in text
    assert coordinator.last_update_success
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.NOT_LOADED


async def test_diagnostics_exclude_personal_data(
    hass, entry, door_payload, camera_device, camera_details
):
    """Diagnostics carry types, states and error class names, never identifying values."""
    personal = {
        "address": "1 Example Street, Springfield",
        "wifi_ssid": "Example Home Wi-Fi",
        "ip_address": "192.0.2.10",
        "parent_serial_number": "TEST-GATEWAY-9",
        "last_updated_by": "Example Person",
        "email": "owner@example.invalid",
    }
    door_payload["state"].update(personal)
    door = Device.parse("test-account", door_payload)
    camera_details["net"] = {"eth": {"mac": "02:00:00:00:00:01"}}
    camera_details["hw"] = {"serialNumber": "TEST-CAM-HW-7", "firmwareVer": "1.2.3"}
    await setup(hass, entry, door, camera_device, camera_details)
    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    identifying = [
        *personal.values(),
        door.serial,
        door.account_id,
        door.name,
        camera_device.serial,
        camera_device.name,
        camera_details["id"],
        "02:00:00:00:00:01",
        "TEST-CAM-HW-7",
        entry.data["username"],
        entry.unique_id,
        "test-access",
        "test-refresh",
    ]
    assert [value for value in identifying if value in text] == []


async def test_device_settings_platform_hooks_and_services(
    hass, entry, door, camera_device, camera_details
):
    camera_details["cap"]["statusLed"] = {"ledOnOff": True}
    camera_details["settings"]["statusLed"] = {"status": "ON"}
    camera_details["cap"]["cam"]["ir"] = True
    camera_details["settings"]["cam"]["ir"] = 2
    camera_details["cap"]["lightList"] = [{"switchStateList": ["on", "off"]}]
    camera_details["settings"]["lightList"] = [
        {"switchState": "off", "intensity": 50, "motionTrigger": False, "triggerDuration": "PT1M"}
    ]
    settings = {
        "light_timer_setting": 150,
        "time_to_close_setting": 300,
        "enable_pir_control": True,
        "enable_motion_wl": True,
    }
    owner = {
        "test-account": AccountAccess("test-account", "owner", {"devices": frozenset({"All"})})
    }
    with (
        patch(
            "custom_components.myq.api.garage.GarageSettings.read",
            AsyncMock(side_effect=lambda device: settings if device.is_door else {}),
        ),
        patch(
            "custom_components.myq.api.management.Management.account_access",
            AsyncMock(return_value=owner),
        ),
    ):
        coordinator = await setup(hass, entry, door, camera_device, camera_details)
    select = {state.attributes["friendly_name"]: state for state in hass.states.async_all("select")}
    assert len(select) == 2
    timer = next(state for name, state in select.items() if "Opener light timer" in name)
    night = next(state for name, state in select.items() if "Night vision" in name)
    assert timer.state == "150" and night.state == "auto"
    assert len(hass.states.async_all("light")) == 1
    close = next(
        state
        for state in hass.states.async_all("number")
        if "Time to close" in state.attributes["friendly_name"]
    )
    assert close.state == "5"
    with (
        patch.object(coordinator.garage, "set_light_timer", AsyncMock()) as set_timer,
        patch.object(coordinator.camera_settings, "set", AsyncMock()) as set_camera,
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": timer.entity_id, "option": "210"},
            blocking=True,
        )
        set_timer.assert_awaited_once_with(door, 210)
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": night.entity_id, "option": "off"},
            blocking=True,
        )
        set_camera.assert_awaited_once_with(camera_device, "night_vision", "off")
    coordinator.async_set_updated_data(Snapshot({door.key: door, camera_device.key: camera_device}))
    await hass.async_block_till_done()
    assert hass.states.get(timer.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(night.entity_id).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_new_device_is_discovered_and_removed_device_is_unavailable(
    hass, entry, door, door_payload
):
    from custom_components.myq.api.models import Device
    from custom_components.myq.coordinator import Snapshot

    coordinator = await setup(hass, entry, door)
    first_id = hass.states.async_all("cover")[0].entity_id
    door_payload["serial_number"] = "NEW-DOOR"
    door_payload["name"] = "Second door"
    new = Device.parse("test-account", door_payload)
    coordinator.async_set_updated_data(Snapshot({new.key: new}))
    await hass.async_block_till_done()
    assert len(hass.states.async_all("cover")) == 2
    assert hass.states.get(first_id).state == STATE_UNAVAILABLE
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("closed", "closed"),
        ("OPEN", "open"),
        ("opening", "opening"),
        ("closing", "closing"),
        ("stopped", "open"),
        ("moving", "unknown"),
        (None, "unknown"),
        ({}, "unknown"),
        ([], "unknown"),
    ],
)
async def test_cover_reports_server_state_without_guessing(
    hass, entry, door_payload, value, expected
):
    door_payload["state"]["door_state"] = value
    await setup(hass, entry, Device.parse("test-account", door_payload))
    cover = hass.states.async_all("cover")[0]
    assert cover.state == expected
    assert cover.attributes["supported_features"] == 3  # OPEN | CLOSE; no STOP or position.
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "action,initial,reported",
    [("open", "closed", "opening"), ("close", "open", "closing"), ("open", "closed", "closed")],
)
async def test_cover_service_sends_once_and_refreshes_observed_state(
    hass, entry, door_payload, action, initial, reported
):
    door_payload["state"]["door_state"] = initial
    door = Device.parse("test-account", door_payload)
    coordinator = await setup(hass, entry, door)
    cover_id = hass.states.async_all("cover")[0].entity_id
    updated = Device.parse(
        door.account_id,
        {**door_payload, "state": {**door_payload["state"], "door_state": reported}},
    )
    with (
        patch.object(
            coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
        ) as request,
        patch.object(coordinator.client, "devices", AsyncMock(return_value=[updated])) as read,
    ):
        await hass.services.async_call(
            "cover", f"{action}_cover", {"entity_id": cover_id}, blocking=True
        )
        await hass.async_block_till_done()
    request.assert_awaited_once()
    assert request.call_args.args == (
        "PUT",
        "https://devices-action.myq-cloud.com/api/v1.0/accounts/test-account/devices/TEST-GDO-1",
    )
    assert request.call_args.kwargs["json"]["action"] == action
    read.assert_awaited_once_with("test-account")  # The follow-up reads one account.
    # A successful HTTP acknowledgment alone must not imply that the door moved.
    assert hass.states.get(cover_id).state == reported
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("action", ["open", "close"])
async def test_cover_service_honors_updated_permissions(hass, entry, door, door_payload, action):
    coordinator = await setup(hass, entry, door)
    cover_id = hass.states.async_all("cover")[0].entity_id
    door_payload["state"][f"is_unattended_{action}_allowed"] = False
    restricted = Device.parse(door.account_id, door_payload)
    coordinator.async_set_updated_data(Snapshot({restricted.key: restricted}))
    await hass.async_block_till_done()
    with patch.object(coordinator.client.http, "request", AsyncMock()) as request:
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "cover", f"{action}_cover", {"entity_id": cover_id}, blocking=True
            )
    request.assert_not_awaited()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("action", ["open", "close"])
async def test_uncertain_cover_command_refreshes_state_without_retry(hass, entry, door, action):
    coordinator = await setup(hass, entry, door)
    cover_id = hass.states.async_all("cover")[0].entity_id
    with (
        patch.object(
            coordinator.client.http, "request", AsyncMock(return_value=Response(503, {}, b""))
        ) as request,
        patch.object(coordinator.client, "devices", AsyncMock(return_value=[door])) as read,
    ):
        with pytest.raises(HomeAssistantError, match="outcome is unknown"):
            await hass.services.async_call(
                "cover", f"{action}_cover", {"entity_id": cover_id}, blocking=True
            )
        await hass.async_block_till_done()
    request.assert_awaited_once()
    read.assert_awaited_once_with("test-account")
    assert hass.states.get(cover_id).state == STATE_CLOSED
    await hass.config_entries.async_unload(entry.entry_id)


async def test_camera_outage_does_not_take_door_offline(hass, entry, door, camera_device):
    with (
        patch(
            "custom_components.myq.api.client.Client.discover",
            AsyncMock(
                return_value={
                    door.key: door,
                    camera_device.key: camera_device,
                }
            ),
        ),
        patch(
            "custom_components.myq.api.video.Video.details",
            AsyncMock(side_effect=AccessDenied("Video denied")),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.async_all("cover")[0].state == STATE_CLOSED
    assert hass.states.async_all("camera")[0].state == STATE_UNAVAILABLE
    await hass.config_entries.async_unload(entry.entry_id)


async def test_entry_left_by_the_former_core_integration_is_refused(hass):
    """Home Assistant's MyQ integration (removed in 2023) stored a username and password."""
    old = MockConfigEntry(
        domain=DOMAIN,
        title="someone@example.invalid",
        data={"username": "someone@example.invalid", "password": "not-used"},
    )
    old.add_to_hass(hass)
    discover = AsyncMock()
    with patch("custom_components.myq.api.client.Client.discover", discover):
        assert not await hass.config_entries.async_setup(old.entry_id)
        await hass.async_block_till_done()
    assert old.state == ConfigEntryState.SETUP_ERROR
    assert old.error_reason_translation_key == "legacy_entry"
    assert old.reason is not None and "former MyQ integration" in old.reason
    discover.assert_not_awaited()
    assert not hass.config_entries.flow.async_progress()  # no reauth: it cannot adopt the entry
    # Removing it must not try to end a MyQ session it never had.
    with patch("custom_components.myq.Client") as client:
        assert await hass.config_entries.async_remove(old.entry_id)
    client.assert_not_called()


async def test_auth_failure_starts_reauth(hass, entry):
    with patch(
        "custom_components.myq.api.client.Client.discover",
        AsyncMock(side_effect=AuthenticationError("Session revoked")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"].get("source") == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_config_flow_saves_only_tokens_and_verified_identity(
    hass, tokens, app_check_registration_file
):
    with (
        patch("custom_components.myq.api.auth.Login.start", AsyncMock(return_value=tokens)),
        patch(
            "custom_components.myq.api.client.Client.profile",
            AsyncMock(
                return_value={
                    "user_id": "verified-user",
                    "email": "test@example.invalid",
                }
            ),
        ),
        patch("custom_components.myq.api.client.Client.discover", AsyncMock(return_value={})),
        patch("custom_components.myq.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        # Mock Login.start must mimic the real token manager update.
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        original_new = flow._new_client

        async def with_tokens():
            client = await original_new()
            client.auth.tokens = tokens
            return client

        with patch.object(flow, "_new_client", with_tokens):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    "username": "test@example.invalid",
                    "password": "test-password-never-save",
                },
            )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == "verified-user"
    assert "password" not in result["data"]
    assert "test-password-never-save" not in str(result["data"])
    assert result["data"]["tokens"] == tokens.as_dict()


async def test_config_flow_requires_mfa_before_creating_entry(
    hass, tokens, app_check_registration_file
):
    from urllib.parse import parse_qs, urlsplit

    from custom_components.myq.api.app_check import AppCheckProof
    from custom_components.myq.api.http import Response

    from .conftest import app_check_jwt, json_response
    from .test_auth import MFA_PAGE, PASSWORD_PAGE

    state = None

    async def request(_self, method, url, **kwargs):
        nonlocal state
        if method == "GET":
            state = parse_qs(urlsplit(url).query)["state"][0]
            return Response(200, {}, PASSWORD_PAGE)
        data = kwargs["data"]
        if isinstance(data, bytes):
            data = {key: values[0] for key, values in parse_qs(data.decode()).items()}
        if "Password" in data or data.get("Code") == "wrong-code":
            return Response(200, {}, MFA_PAGE)
        if "Code" in data:
            return Response(302, {"Location": f"com.myqops://android?code=test&state={state}"}, b"")
        return json_response(
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_in": 3600,
            }
        )

    with (
        patch("custom_components.myq.api.android_http.AndroidHttp.request", request),
        patch(
            "custom_components.myq.api.firebase.FirebaseDebugAppCheckProvider.acquire",
            AsyncMock(return_value=AppCheckProof.from_token(app_check_jwt())),
        ),
        patch(
            "custom_components.myq.api.client.Client.profile",
            AsyncMock(return_value={"user_id": "verified-user"}),
        ),
        patch("custom_components.myq.api.client.Client.discover", AsyncMock(return_value={})),
        patch("custom_components.myq.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": "test@example.invalid",
                "password": "private-password",
            },
        )
        assert result["step_id"] == "mfa"
        assert not hass.config_entries.async_entries(DOMAIN)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "wrong-code"}
        )
        assert result["step_id"] == "mfa" and result["errors"]["base"] == "invalid_code"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"response": "123456"}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert "private-password" not in str(result["data"])
    assert "123456" not in str(result["data"])
    assert result["data"]["tokens"]["refresh_token"] == tokens.refresh_token


@pytest.mark.parametrize(
    "user_id,reason",
    [
        ("test-user", "reauth_successful"),
        ("different-user", "unique_id_mismatch"),
    ],
)
async def test_reauth_preserves_account_identity(
    hass, entry, tokens, user_id, reason, app_check_registration_file
):
    async def start(login, username, password):
        login.token_manager.tokens = tokens
        return tokens

    with (
        patch("custom_components.myq.api.auth.Login.start", start),
        patch(
            "custom_components.myq.api.client.Client.profile",
            AsyncMock(return_value={"user_id": user_id, "email": "test@example.invalid"}),
        ),
        patch("custom_components.myq.api.client.Client.discover", AsyncMock(return_value={})),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": "reauth",
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": "test@example.invalid",
                "password": "private-password",
            },
        )
        await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == reason
    assert entry.unique_id == "test-user"
    assert reload.await_count == (1 if reason == "reauth_successful" else 0)


async def test_refresh_rotation_persists_without_reloading_but_options_reload(hass, entry, door):
    from custom_components.myq.api.tokens import Tokens

    coordinator = await setup(hass, entry, door)
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        rotated = Tokens("new-access", "new-refresh", 9999999999)
        await coordinator.client.auth.on_update(rotated)
        await hass.async_block_till_done()
        assert entry.data["tokens"] == rotated.as_dict()
        reload.assert_not_awaited()
        hass.config_entries.async_update_entry(entry, options={"poll_interval": 120})
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_flow_rate_limit_survives_another_login_attempt(hass, app_check_registration_file):
    from custom_components.myq.api.errors import RateLimited

    with patch(
        "custom_components.myq.api.auth.Login.start",
        AsyncMock(side_effect=RateLimited(120, server_retry_after=True)),
    ) as start:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    "username": "test@example.invalid",
                    "password": "private-password",
                },
            )
            assert result["step_id"] == "user"
            assert result["errors"]["base"] == "rate_limited"
        start.assert_awaited_once()
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()


async def _open_cover_with_error(hass, entry, door, error):
    """Send a cover command that fails with the given error; report whether reauth began."""
    await setup(hass, entry, door)
    cover = hass.states.async_all("cover")[0].entity_id
    coordinator = entry.runtime_data
    with (
        patch(
            "custom_components.myq.api.client.Client.command",
            AsyncMock(side_effect=error),
        ),
        patch.object(coordinator, "track") as track,
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call("cover", "open_cover", {"entity_id": cover}, blocking=True)
    await hass.async_block_till_done()
    reauth = any(
        flow["context"].get("source") == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )
    await hass.config_entries.async_unload(entry.entry_id)
    return reauth, track.called


async def test_command_401_after_renewal_fails_only_that_command(hass, entry, door):
    # ft0/c1 gives up after one renewal; the app stays signed in. MyQ did not act, so
    # the door is not watched for a change either.
    reauth, tracked = await _open_cover_with_error(hass, entry, door, RequestUnauthorized())
    assert not reauth and not tracked


async def test_refused_renewal_before_a_command_keeps_the_session(hass, entry, door):
    reauth, tracked = await _open_cover_with_error(hass, entry, door, SessionRenewalFailed())
    assert not reauth and not tracked


async def test_command_when_myq_ended_the_session_starts_reauth(hass, entry, door):
    error = AuthenticationError("MyQ ended this session (invalid_grant); sign in again")
    reauth, _ = await _open_cover_with_error(hass, entry, door, error)
    assert reauth


async def test_live_card_is_auto_loaded_from_a_cacheable_versioned_url(
    hass, entry, door, hass_client
):
    from custom_components.myq import CARD_URL, LEGACY_CARD_URL
    from custom_components.myq.api.const import VERSION

    hass.config.components.add("frontend")
    with patch("homeassistant.components.frontend.add_extra_js_url") as add:
        await setup(hass, entry, door)
    add.assert_called_once_with(hass, CARD_URL)
    assert VERSION in CARD_URL
    client = await hass_client()
    versioned, legacy = await client.get(CARD_URL), await client.get(LEGACY_CARD_URL)
    assert versioned.status == legacy.status == 200
    assert "max-age" in versioned.headers.get("Cache-Control", "")
    assert "max-age" not in legacy.headers.get("Cache-Control", "")  # Manual resources stay fresh.
    assert await versioned.text() == await legacy.text()
    await hass.config_entries.async_unload(entry.entry_id)


# --- Session lifetime follows the app: only MyQ ending the session needs a new sign-in ----


async def test_refused_renewal_keeps_polling_without_reauth(hass, entry, door):
    coordinator = await setup(hass, entry, door)
    coordinator.client.auth.tokens = Tokens("expired", "kept-refresh", 0)
    with patch(
        "custom_components.myq.api.android_http.AndroidHttp.request",
        AsyncMock(return_value=json_response({"error": "invalid_request"}, 400)),
    ):
        await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert isinstance(coordinator.last_exception.__cause__, SessionRenewalFailed)
    assert not any(
        flow["context"].get("source") == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )
    assert coordinator.client.auth.tokens.refresh_token == "kept-refresh"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_lingering_401_after_renewal_does_not_start_reauth(hass, entry, door):
    coordinator = await setup(hass, entry, door)
    renewed = json_response({"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    answers = [Response(401, {}, b""), renewed] + [Response(401, {}, b"")] * 20

    async def request(method, url, **kwargs):
        return answers.pop(0) if answers else Response(401, {}, b"")

    with patch("custom_components.myq.api.android_http.AndroidHttp.request", side_effect=request):
        coordinator._last_full = 0  # Force a full poll through Client.request.
        await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception.__cause__, RequestUnauthorized)
    assert coordinator.client.auth.tokens.refresh_token == "r2"  # Renewed exactly once.
    assert not any(
        flow["context"].get("source") == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )
    await hass.config_entries.async_unload(entry.entry_id)


async def test_removing_the_entry_revokes_its_session(hass, entry, door, tokens):
    from urllib.parse import parse_qs

    await setup(hass, entry, door)
    with patch(
        "custom_components.myq.api.android_http.AndroidHttp.request",
        AsyncMock(return_value=Response(200, {}, b"")),
    ) as request:
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    revocations = [c for c in request.call_args_list if c.args[1] == REVOCATION_URL]
    assert len(revocations) == 1
    assert parse_qs(revocations[0].kwargs["data"].decode())["token"] == [tokens.refresh_token]
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_removal_does_not_depend_on_reaching_myq(hass, entry, door):
    await setup(hass, entry, door)
    with patch(
        "custom_components.myq.api.android_http.AndroidHttp.request",
        AsyncMock(side_effect=CannotConnect("offline")),
    ):
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_rate_limited_command_is_not_watched_as_if_it_might_have_run(hass, entry, door):
    """A 429, or a request held back during a cooldown, was not acted on by MyQ."""
    from custom_components.myq.api.errors import RateLimited

    reauth, tracked = await _open_cover_with_error(hass, entry, door, RateLimited(60))
    assert not reauth and not tracked

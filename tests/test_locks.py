"""Synthetic lock contracts; no live accounts or physical commands."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.client import Client
from custom_components.myq.api.const import NATIVE_HEADERS
from custom_components.myq.api.errors import (
    CommandRejected,
    CommandUncertain,
    RequestUnauthorized,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.locks import Locks, is_lock, lock_state
from custom_components.myq.api.models import Device
from custom_components.myq.const import DOMAIN
from custom_components.myq.coordinator import Snapshot


def make_lock(*, kind="lockitron", platform="", gateway="test-gateway", **state):
    return Device.parse(
        "test-account",
        {
            "serial_number": "test-lock",
            "name": "Test lock",
            "device_type": kind,
            "device_family": "locks",
            "device_platform": platform,
            "gateway_id": gateway,
            "state": {
                "online": True,
                "lock_state": "locked",
                "bluetooth_connectability": "remote",
                **state,
            },
        },
    )


@pytest.mark.parametrize("action", ["lock", "unlock"])
@pytest.mark.parametrize(
    "kind,platform,method,url,extra",
    [
        (
            "lockitron",
            "",
            "POST",
            "https://devices-locks.myq-cloud.com/api/v6.0/accounts/test-account/"
            "devices/locks/test-lock/",
            {"params": {"requestRemote": "true"}},
        ),
        (
            "vendor_lock",
            "",
            "POST",
            "https://devices-locks.myq-cloud.com/api/v6.0/accounts/test-account/"
            "devices/locks/test-lock/",
            {"params": {"requestRemote": "true"}},
        ),
        (
            "vendor_lock",
            "seam",
            "PUT",
            "https://devices-action.myq-cloud.com/api/v1.0/accounts/test-account/devices/test-lock",
            {},
        ),
        (
            "wifilockgateway",
            "",
            "PUT",
            "https://devices-action.myq-cloud.com/api/v1.0/accounts/test-account/devices/test-lock",
            {},
        ),
    ],
)
async def test_lock_wire_contract(tokens, http, action, kind, platform, method, url, extra):
    http.request.return_value = Response(204, {}, b"")
    client = Client(transport=http, tokens=tokens)
    device = make_lock(kind=kind, platform=platform)
    await Locks(client).command(device, action)
    expected = {**extra, "headers": {**NATIVE_HEADERS, "Authorization": "Bearer test-access"}}
    if method == "PUT":
        expected["json"] = {"action": action}
    else:
        url += action
    http.request.assert_awaited_once_with(method, url, **expected)
    assert device.state["lock_state"] == "locked"


@pytest.mark.parametrize(
    "changes",
    [
        {"online": False},
        {"online": None},
        {"online": "true"},
        {"gateway": None},
        {"gateway": {}},
        {"bluetooth_connectability": "local"},
        {"bluetooth_connectability": None},
        {"bluetooth_connectability": []},
        {"monitor_only_mode": True},
        {"monitor_only_mode": "false"},
        {"attached_work_light_error_present": True},
        {"attached_work_light_error_present": "false"},
        {"is_unattended_close_allowed": False},
        {"is_unattended_close_allowed": None},
        {"lock_state": "locking"},
        {"lock_state": "unlocking"},
        {"lock_state": "jammed"},
        {"lock_state": []},
        {"lock_state": None},
        {"kind": "smartlock"},
        {"kind": "smartlockcam"},
        {"kind": "smartlockcam1"},
        {"kind": "smartlockanzcam"},
        {"kind": "wifilockgateway", "camera_online": False},
        {"kind": "wifilockgateway", "camera_online": "true"},
    ],
)
async def test_lock_rejects_unconfirmed_control(tokens, http, changes):
    with pytest.raises(CommandRejected):
        await Locks(Client(transport=http, tokens=tokens)).command(make_lock(**changes), "unlock")
    http.request.assert_not_awaited()


@pytest.mark.parametrize("kind,platform", [("wifilockgateway", ""), ("vendor_lock", "seam")])
async def test_cloud_only_types_need_no_bluetooth_gateway(tokens, http, kind, platform):
    http.request.return_value = Response(204, {}, b"")
    await Locks(Client(transport=http, tokens=tokens)).command(
        make_lock(kind=kind, platform=platform, gateway=None, bluetooth_connectability=None),
        "unlock",
    )
    assert http.request.call_args.args[0] == "PUT"


async def test_legacy_busy_connection_is_remote(tokens, http):
    http.request.return_value = Response(204, {}, b"")
    await Locks(Client(transport=http, tokens=tokens)).command(
        make_lock(bluetooth_connectability="busy"), "lock"
    )
    assert http.request.call_args.kwargs["params"] == {"requestRemote": "true"}


@pytest.mark.parametrize("action", ["open", "toggle", "", None, []])
async def test_lock_rejects_unknown_action(tokens, http, action):
    with pytest.raises(CommandRejected):
        await Locks(Client(transport=http, tokens=tokens)).command(make_lock(), action)
    http.request.assert_not_awaited()


async def test_removed_lock_rejected(tokens, http):
    with pytest.raises(CommandRejected):
        await Locks(Client(transport=http, tokens=tokens)).command(None, "unlock")
    http.request.assert_not_awaited()


@pytest.mark.parametrize("status,error", [(401, RequestUnauthorized), (503, CommandUncertain)])
async def test_lock_mutation_is_never_replayed(tokens, http, status, error):
    # A 401 with a code other than 401.102/401 is not renewed (ft0/c1), so not resent.
    body = b'{"code": "401.122"}' if status == 401 else b""
    http.request.return_value = Response(status, {}, body)
    with pytest.raises(error):
        await Locks(Client(transport=http, tokens=tokens)).command(make_lock(), "unlock")
    http.request.assert_awaited_once()


async def test_lock_mutations_cannot_overlap_across_helper_instances(tokens, http):
    started = asyncio.Event()
    release = asyncio.Event()

    async def response(*args, **kwargs):
        started.set()
        await release.wait()
        return Response(204, {}, b"")

    http.request.side_effect = response
    client = Client(transport=http, tokens=tokens)
    pending = asyncio.create_task(Locks(client).command(make_lock(), "lock"))
    await started.wait()
    try:
        with pytest.raises(CommandRejected, match="already in progress"):
            await Locks(client).command(make_lock(), "unlock")
    finally:
        release.set()
        await pending
    http.request.assert_awaited_once()


@pytest.mark.parametrize(
    "value,expected",
    [("LOCKED", "locked"), ("Unlocking", "unlocking"), ("jammed", None), ({}, None)],
)
def test_lock_state_normalization(value, expected):
    assert lock_state(make_lock(lock_state=value)) == expected


def test_lock_camera_and_control_records_have_distinct_types():
    assert is_lock(make_lock(kind="wifilockgateway"))
    for kind in ("smartlockcam", "smartlockcam1", "smartlockanzcam"):
        camera = make_lock(kind=kind)
        assert camera.is_camera
        assert not is_lock(camera)


@pytest.fixture
async def lock_setup(hass, tokens, enable_custom_integrations):
    entries = []

    async def setup(device):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="test-user",
            data={"username": "test@example.invalid", "tokens": tokens.as_dict()},
        )
        entry.add_to_hass(hass)
        entries.append(entry)
        with patch.object(Client, "discover", AsyncMock(return_value={device.key: device})):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        return entry.runtime_data

    yield setup
    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("locked", "locked"),
        ("unlocked", "unlocked"),
        ("locking", "locking"),
        ("unlocking", "unlocking"),
        (None, "unknown"),
    ],
)
async def test_ha_lock_observes_cloud_state(hass, lock_setup, value, expected):
    await lock_setup(make_lock(lock_state=value))
    entities = hass.states.async_all("lock")
    assert len(entities) == 1
    assert entities[0].state == expected
    assert entities[0].attributes["supported_features"] == 0


async def test_ha_bluetooth_only_lock_has_no_lock_entity(hass, lock_setup):
    # No gateway and no action API: HA can never command it; its sensors remain.
    await lock_setup(make_lock(gateway=None, bluetooth_connectability="local"))
    assert hass.states.async_all("lock") == []
    assert hass.states.async_all("sensor")


async def test_ha_gateway_lock_out_of_remote_range_is_unavailable(hass, lock_setup):
    await lock_setup(make_lock(bluetooth_connectability="local"))
    assert hass.states.async_all("lock")[0].state == "unavailable"


async def test_pin_lockout_blocks_lock_commands(hass, lock_setup):
    device = make_lock(failed_credential_lockout_active=True)
    coordinator = await lock_setup(device)
    with patch.object(coordinator.client.http, "request", AsyncMock()) as request:
        with pytest.raises(CommandRejected, match="locked out"):
            await Locks(coordinator.client).command(device, "unlock")
    request.assert_not_awaited()
    assert hass.states.get("binary_sensor.test_lock_pin_lockout").state == "on"


async def test_ha_lock_service_refreshes_without_optimistic_state(hass, lock_setup):
    device = make_lock()
    coordinator = await lock_setup(device)
    lock_id = hass.states.async_all("lock")[0].entity_id
    with (
        patch.object(
            coordinator.client.http, "request", AsyncMock(return_value=Response(204, {}, b""))
        ) as request,
        patch.object(coordinator.client, "devices", AsyncMock(return_value=[device])) as read,
    ):
        await hass.services.async_call("lock", "unlock", {"entity_id": lock_id}, blocking=True)
        await hass.async_block_till_done()
    request.assert_awaited_once()
    read.assert_awaited_once_with(device.account_id)
    assert request.call_args.args[1].endswith("/unlock")
    assert hass.states.get(lock_id).state == "locked"


async def test_ha_lock_service_honors_latest_permission(hass, lock_setup):
    coordinator = await lock_setup(make_lock())
    lock_id = hass.states.async_all("lock")[0].entity_id
    restricted = make_lock(monitor_only_mode=True)
    coordinator.async_set_updated_data(Snapshot({restricted.key: restricted}))
    await hass.async_block_till_done()
    with (
        patch.object(coordinator.client.http, "request", AsyncMock()) as request,
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call("lock", "unlock", {"entity_id": lock_id}, blocking=True)
    request.assert_not_awaited()

"""Synthetic management contracts, authorization changes and confirmed outcomes."""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.myq.api.account_access import parse_account_access
from custom_components.myq.api.client import Client
from custom_components.myq.api.const import GDO, LOCKS, NATIVE_HEADERS
from custom_components.myq.api.errors import (
    CannotConnect,
    CommandRejected,
    CommandUncertain,
    ProtocolError,
)
from custom_components.myq.api.management import (
    Management,
    WorkflowSchedule,
    _overlaps,
    can_auto_relock,
    can_create_workflow,
    shareable,
)
from custom_components.myq.api.models import Device
from custom_components.myq.management_entities import (
    management_select_entities,
    management_switch_entities,
)

from .conftest import json_response
from .test_locks import make_lock


def access_payload(role="owner", **permissions):
    return {
        "account_access": [
            {
                "account_id": "test-account",
                "role": role,
                "permissions": permissions
                or {"devices": ["All"], "guests": ["All"], "schedules": ["All"]},
            }
        ]
    }


def accessory_payload(door, *, name="Remote", deleting=False, serial="TX02-test"):
    return {
        "gdo_serial_number": door.serial,
        "gdo_transmitter_names": [
            {
                "name": name,
                "tx_serial_number": serial,
                "transmitter_type": "OokTx",
                "is_delete_in_progress": deleting,
                "battery_state": "good",
            }
        ],
    }


def scene_payload(*, enabled=True, configuration="config-1", schedule="schedule-1", **changes):
    return {
        "items": [
            {
                "configuration_id": configuration,
                "schedules": [
                    {
                        "id": schedule,
                        "label": "Evening",
                        "enabled": enabled,
                        "days": "monday, friday",
                        "start_time": "18:30:00",
                        "end_time": "22:00:00",
                        **changes,
                    }
                ],
            }
        ]
    }


def make_management(tokens, http, device):
    client = Client(transport=http, tokens=tokens)
    client.devices = AsyncMock(return_value=[device])
    management = Management(client)
    management.poll_interval = 0
    management.poll_attempts = 2
    return management


@pytest.mark.parametrize(
    "role,devices,history,schedules",
    [
        ("owner", True, True, True),
        ("Owner", True, True, True),
        ("co_owner", True, True, False),
        ("family", True, True, False),
        ("guest", True, False, False),
        ("quick_pass", False, False, False),
        ("new_admin", False, False, False),
        (None, False, False, False),
        ([], False, False, False),
    ],
)
def test_roles_follow_the_apps_screens_not_grants(role, devices, history, schedules):
    # cn0/a parses the permission lists, but nothing in the app reads them.
    access = parse_account_access(access_payload(role, devices=[]))["test-account"]
    assert access.allows("devices", "Control") is devices
    assert access.can_control_devices is devices
    assert access.allows("history", "View") is history
    assert access.allows("schedules", "Manage") is schedules
    assert not access.allows("devices", "Unknown")


def test_owner_comes_from_created_by_as_in_the_app():
    # h0.k: the account the signed-in user created is theirs, whatever the role says.
    access = parse_account_access(access_payload("family"), owned={"test-account", "other"})
    assert access["test-account"].owner and access["test-account"].owner_or_co_owner
    assert access["other"].owner and access["other"].allows("schedules", "Manage")
    assert not parse_account_access(access_payload("family"))["test-account"].owner


async def test_owner_is_known_from_accounts_list(tokens, http):
    client = Client(transport=http, tokens=tokens)
    http.request.return_value = json_response(
        {
            "accounts": [
                {"id": "test-account", "name": "Home", "created_by": "test-user"},
                {"id": "shared", "name": "Cabin", "created_by": "someone-else"},
            ]
        }
    )
    await client.accounts()
    management = Management(client, user_id="test-user")
    assert management.owned_accounts() == {"test-account"}
    assert set(management.owner_access()) == {"test-account"}
    assert Management(client).owned_accounts() == frozenset()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"account_access": {}},
        {"account_access": [{"account_id": "x", "permissions": {"devices": "All"}}]},
        {"account_access": [{"account_id": "x"}, {"account_id": "x"}]},
    ],
)
def test_permissions_fail_closed(payload):
    with pytest.raises(ProtocolError):
        parse_account_access(payload)


def test_permissions_repr_excludes_account_and_grants():
    access = parse_account_access(access_payload())["test-account"]
    assert access.can_control_devices and access.owner
    assert "test-account" not in repr(access)


async def test_auto_relock_exact_request_and_confirmed_state(tokens, http):
    device = make_lock(allow_auto_relock=True, auto_relock_enabled=False, auto_relock_time=30)
    confirmed = make_lock(allow_auto_relock=True, auto_relock_enabled=True, auto_relock_time=90)
    manager = make_management(tokens, http, device)
    manager.client.devices.side_effect = [[device], [device], [confirmed]]
    http.request.side_effect = [json_response(access_payload()), json_response({})]
    await manager.set_auto_relock(device, True, 90)
    call = http.request.call_args_list[1]
    assert call.args == (
        "PUT",
        LOCKS + "/api/v6.0/accounts/test-account/devices/locks/test-lock/auto_relock",
    )
    assert call.kwargs == {
        "headers": {**NATIVE_HEADERS, "Authorization": "Bearer test-access"},
        "params": {"requestRemote": "true"},
        "json": {"auto_relock_enabled": True, "auto_relock_time": "90"},
    }
    assert device.state["auto_relock_enabled"] is False


@pytest.mark.parametrize("role", ["co_owner", "guest", "family", "quick_pass"])
async def test_owner_setting_rechecks_current_access(tokens, http, role):
    device = make_lock(allow_auto_relock=True, auto_relock_enabled=False, auto_relock_time=30)
    manager = make_management(tokens, http, device)
    http.request.return_value = json_response(access_payload(role, devices=["All"]))
    with pytest.raises(CommandRejected):
        await manager.set_auto_relock(device, True, 60)
    assert all(c.args[0] == "GET" for c in http.request.call_args_list)


async def test_auto_relock_timeout_no_replay(tokens, http):
    device = make_lock(allow_auto_relock=True, auto_relock_enabled=False, auto_relock_time=30)
    manager = make_management(tokens, http, device)
    http.request.side_effect = [json_response(access_payload()), CannotConnect("Synthetic")]
    with pytest.raises(CommandUncertain):
        await manager.set_auto_relock(device, True, 60)
    assert len([c for c in http.request.call_args_list if c.args[0] == "PUT"]) == 1


def test_auto_relock_entities_drop_availability_when_role_changes():
    device = make_lock(allow_auto_relock=True, auto_relock_enabled=False, auto_relock_time=30)
    access = parse_account_access(access_payload())
    coordinator = SimpleNamespace(
        data=SimpleNamespace(devices={device.key: device}, account_access=access),
        last_update_success=True,
        config_entry=None,
    )
    entity = next(management_switch_entities(coordinator, device))
    assert entity.available
    coordinator.data.account_access = parse_account_access(access_payload("guest"))
    assert not entity.available
    assert not can_auto_relock(make_lock(kind="wifilockgateway"), access["test-account"])


async def test_accessory_rename_wire_and_pending_removal(tokens, http, door):
    manager = make_management(tokens, http, door)
    http.request.side_effect = [
        json_response(accessory_payload(door)),
        json_response(access_payload()),
        json_response({}),
        json_response(accessory_payload(door, name="New")),
    ]
    await manager.rename_accessory(door, "TX02-test", "New")
    call = http.request.call_args_list[2]
    assert call.args == (
        "PUT",
        GDO + "/api/v6.0/accounts/test-account/door_openers/TEST-GDO-1/OokTx/rename",
    )
    assert call.kwargs["json"] == {
        "name": "New",
        "tx_serial_number": "TX02-test",
        "is_delete_in_progress": False,
    }
    http.request.reset_mock()
    http.request.side_effect = [
        json_response(accessory_payload(door)),
        json_response(access_payload()),
        json_response({}),
        json_response(accessory_payload(door, deleting=True)),
        json_response(accessory_payload(door, deleting=True)),
    ]
    with pytest.raises(CommandUncertain):
        await manager.remove_accessory(door, "TX02-test")
    assert len([c for c in http.request.call_args_list if c.args[0] == "PUT"]) == 1


async def test_accessory_response_must_belong_to_opener(tokens, http, door):
    manager = make_management(tokens, http, door)
    payload = accessory_payload(door)
    payload["gdo_serial_number"] = "another-opener"
    http.request.return_value = json_response(payload)
    with pytest.raises(ProtocolError):
        await manager.accessories(door)


async def test_schedule_enable_sends_the_full_native_dto(tokens, http, camera_device):
    manager = make_management(tokens, http, camera_device)
    http.request.side_effect = [
        json_response(scene_payload()),
        json_response(access_payload()),
        json_response({}),
        json_response(scene_payload(enabled=False)),
    ]
    await manager.set_schedule_enabled(
        camera_device, "privacy_mode", "config-1", "schedule-1", False
    )
    call = http.request.call_args_list[2]
    assert call.args == (
        "PUT",
        "https://workflows.myq-cloud.com/api/v1.0/accounts/test-account/"
        "workflows/scenes/config-1/schedules/schedule-1",
    )
    # l60/k2 -> c21/f: the app's only schedule PUT carries all five fields.
    assert call.kwargs["json"] == {
        "label": "Evening",
        "enabled": False,
        "days": "monday, friday",
        "start_time": "18:30:00",
        "end_time": "22:00:00",
    }
    assert http.request.call_args_list[0].kwargs["params"] == {
        "serialNumber": camera_device.serial,
        "feature": "privacy_mode",
    }


async def test_schedule_rejects_unassociated_config_before_mutation(tokens, http, camera_device):
    manager = make_management(tokens, http, camera_device)
    http.request.return_value = json_response(scene_payload(configuration="someone-else"))
    with pytest.raises(CommandRejected):
        await manager.delete_schedule(camera_device, "privacy_mode", "config-1", "schedule-1")
    assert http.request.await_count == 1


async def test_schedule_create_days_order_and_time_contract(tokens, http, camera_device):
    manager = make_management(tokens, http, camera_device)
    after = scene_payload()
    after["items"][0]["schedules"].append(
        {
            "id": "new-schedule",
            "label": "Bedtime",
            "enabled": True,
            "days": "monday, sunday",
            "start_time": "23:00:00",
            "end_time": "07:00:00",
        }
    )
    http.request.side_effect = [
        json_response(scene_payload()),
        json_response(access_payload()),
        json_response({}),
        json_response(after),
    ]
    await manager.save_schedule(
        camera_device,
        "privacy_mode",
        "config-1",
        label="Bedtime",
        days=["sunday", "monday"],
        start_time="23:00:00",
        end_time="07:00:00",
        enabled=True,
    )
    call = http.request.call_args_list[2]
    assert call.args[0] == "POST"
    assert call.kwargs["json"] == {
        "label": "Bedtime",
        "enabled": True,
        "days": "monday, sunday",
        "start_time": "23:00:00",
        "end_time": "07:00:00",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"days": []},
        {"days": ["monday", "monday"]},
        {"start_time": "24:00:00"},
        {"start_time": "12:00:05"},
        {"enabled": "true"},
        {"end_time": "18:30:00"},
        {"days": ["vacation"]},
    ],
)
async def test_schedule_invalid_input_never_sends(tokens, http, camera_device, changes):
    manager = make_management(tokens, http, camera_device)
    kwargs = {
        "label": "Test",
        "days": ["monday"],
        "start_time": "18:30:00",
        "end_time": "20:00:00",
        "enabled": True,
        **changes,
    }
    with pytest.raises(CommandRejected):
        await manager.save_schedule(camera_device, "privacy_mode", "config-1", **kwargs)
    http.request.assert_not_awaited()


def pin_item(status="synced", **kwargs):
    return {"id": "pin-1", "name": "Visitor", "access": "enabled", "status": status, **kwargs}


async def test_pin_create_once_waits_for_synced_and_excludes_secret(tokens, http):
    device = make_lock(allow_pin_code=True)
    manager = make_management(tokens, http, device)
    responses = [
        access_payload(),
        {"items": []},
        access_payload(),
        pin_item("pending"),
        {"items": [pin_item("pending")]},
        {"items": [pin_item()]},
    ]
    sent = []

    async def send(method, url, **kwargs):
        sent.append((method, url, copy.deepcopy(kwargs)))
        return json_response(responses.pop(0))

    http.request.side_effect = send
    await manager.save_pin(device, name="Visitor", pin="001234")
    mutation = next(item for item in sent if item[0] == "POST")
    assert mutation[1].endswith("/api/v6.0/accounts/test-account/devices/locks/test-lock/pins")
    assert mutation[2]["json"] == {"name": "Visitor", "access": "enabled", "pin": "001234"}
    assert mutation[2]["params"] == {"requestRemote": "true"}
    assert "001234" not in repr(manager.__dict__)
    assert "001234" not in repr(http.request.call_args_list)


@pytest.mark.parametrize(
    "status,expected",
    [("pending", CommandUncertain), ("duplicate", CommandRejected), ("failed", CommandRejected)],
)
async def test_pin_pending_and_terminal_failure_are_not_success(tokens, http, status, expected):
    manager = make_management(tokens, http, make_lock(allow_pin_code=True))
    http.request.side_effect = [
        json_response(access_payload()),
        json_response({"items": []}),
        json_response(access_payload()),
        json_response(pin_item("pending")),
        json_response({"items": [pin_item(status)]}),
        json_response({"items": [pin_item(status)]}),
    ]
    with pytest.raises(expected):
        await manager.save_pin(make_lock(allow_pin_code=True), name="Visitor", pin="123456")
    assert len([c for c in http.request.call_args_list if c.args[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "answer,observations,expected",
    [
        # No pending status anywhere: a stale "synced" proves nothing.
        ("synced", [pin_item(), pin_item()], CommandUncertain),
        # The PUT answer itself is pending (c11.Pin), as l31/b polls from.
        ("pending", [pin_item(), pin_item()], None),
        ("pending", [pin_item("pending"), pin_item()], None),
        ("pending", [pin_item("pending"), pin_item(access="disabled")], CommandUncertain),
        ("pending", [pin_item("pending"), pin_item(name="Different")], CommandUncertain),
        ("synced", [pin_item(), pin_item("failed")], CommandRejected),
    ],
)
async def test_pin_replacement_requires_fresh_enabled_synchronization(
    tokens, http, answer, observations, expected
):
    device = make_lock(allow_pin_code=True)
    manager = make_management(tokens, http, device)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response({"items": [pin_item()]}),
        json_response(access_payload()),
        json_response(pin_item(answer)),
        *(json_response({"items": [item]}) for item in observations),
    ]
    if expected is None:
        await manager.save_pin(device, name="Visitor", pin="5678", pin_id="pin-1")
    else:
        with pytest.raises(expected):
            await manager.save_pin(device, name="Visitor", pin="5678", pin_id="pin-1")
    mutations = [call for call in http.request.await_args_list if call.args[0] == "PUT"]
    assert len(mutations) == 1
    assert mutations[0].args[1].endswith("/pins/pin-1")
    assert "5678" not in repr(http.request.call_args_list)


async def test_new_pin_can_confirm_immediate_sync_for_new_identity(tokens, http):
    device = make_lock(allow_pin_code=True)
    manager = make_management(tokens, http, device)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response({"items": []}),
        json_response(access_payload()),
        json_response(pin_item()),
        json_response({"items": [pin_item()]}),
    ]
    await manager.save_pin(device, name="Visitor", pin="1234")
    assert len([c for c in http.request.await_args_list if c.args[0] == "POST"]) == 1


async def test_pin_delete_confirms_absence(tokens, http):
    device = make_lock(allow_pin_code=True)
    manager = make_management(tokens, http, device)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response({"items": [pin_item()]}),
        json_response(access_payload()),
        json_response({}),
        json_response({"items": []}),
    ]
    await manager.delete_pin(device, "pin-1")
    call = http.request.call_args_list[3]
    assert call.args == (
        "DELETE",
        LOCKS + "/api/v6.0/accounts/test-account/devices/locks/test-lock/pins/pin-1",
    )
    assert "json" not in call.kwargs


async def test_overlapping_management_is_rejected_without_queue(tokens, http, door):
    manager = make_management(tokens, http, door)
    started, release = asyncio.Event(), asyncio.Event()

    async def waiting(*_):
        started.set()
        await release.wait()
        return [door]

    manager.client.devices.side_effect = waiting
    http.request.return_value = json_response(accessory_payload(door, deleting=True))
    task = asyncio.create_task(manager.rename_accessory(door, "TX02-test", "One"))
    await started.wait()
    with pytest.raises(CommandRejected, match="already in progress"):
        await manager.rename_accessory(door, "TX02-test", "Two")
    release.set()
    with pytest.raises(CommandRejected):
        await task


def guest_payload(*, allowed=None, role="guest", user="guest-1"):
    return {
        "account_users": [
            {
                "id": user,
                "guest_name": "Visitor",
                "role": role,
                "email": "sensitive@example.invalid",
                "date_of_birth": "sensitive-birthday",
                "access_schedules": [
                    {
                        "id": "access-1",
                        "schedule_type": "recurring",
                        "timezone": "America/New_York",
                        "recurring_type": "weekly",
                        "recurring_value": "M,W,F",
                        "recurring_start_time": "08:00:00",
                        "recurring_end_time": "18:00:00",
                        "allowed_devices": ["TEST-GDO-1"] if allowed is None else allowed,
                    }
                ],
            }
        ]
    }


async def test_guest_access_edits_preserve_schedule_and_drop_private_fields(tokens, http, door):
    manager = make_management(tokens, http, door)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response(guest_payload()),
        json_response(access_payload()),
        json_response({}),
        json_response(guest_payload(allowed=[])),
    ]
    await manager.set_guest_devices(door, "guest-1", "access-1", [])
    call = http.request.call_args_list[3]
    assert call.args == (
        "PUT",
        "https://guestaccess.myq-cloud.com/api/v6.0/accounts/test-account/"
        "users/guest-1/accessSchedules/access-1",
    )
    assert call.kwargs["json"] == {
        "schedule_type": "recurring",
        "timezone": "America/New_York",
        "recurring_type": "weekly",
        "recurring_value": "M,W,F",
        "recurring_start_time": "08:00:00",
        "recurring_end_time": "18:00:00",
        "allowed_devices": [],
    }


async def test_guest_associations_cannot_grant_unknown_account_device(tokens, http, door):
    manager = make_management(tokens, http, door)
    with pytest.raises(CommandRejected, match="must belong"):
        await manager.set_guest_devices(door, "guest-1", "access-1", ["another-account-device"])
    http.request.assert_not_awaited()


@pytest.mark.parametrize("role", ["owner", "co_owner", "family", "unknown"])
async def test_guest_removal_never_removes_owners_or_unrecognized_roles(tokens, http, door, role):
    manager = make_management(tokens, http, door)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response(guest_payload(role=role)),
    ]
    with pytest.raises(CommandRejected, match="Only an existing guest"):
        await manager.remove_guest(door, "guest-1")
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


async def test_guest_removal_confirmed_by_inventory(tokens, http, door):
    manager = make_management(tokens, http, door)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response(guest_payload()),
        json_response(access_payload()),
        json_response({}),
        json_response({"account_users": []}),
    ]
    await manager.remove_guest(door, "guest-1")
    call = http.request.call_args_list[3]
    assert call.args == (
        "DELETE",
        "https://guestaccess.myq-cloud.com/api/v6.0/accounts/test-account/users/guest-1",
    )
    assert "json" not in call.kwargs


async def test_accessory_assignment_uses_native_dto_and_confirmed_user(tokens, http, door):
    manager = make_management(tokens, http, door)
    after = accessory_payload(door)
    after["gdo_transmitter_names"][0]["account_user_id"] = "guest-1"
    http.request.side_effect = [
        json_response(accessory_payload(door)),
        json_response(access_payload()),
        json_response(guest_payload()),
        json_response(access_payload()),
        json_response({}),
        json_response(after),
    ]
    await manager.assign_accessory(door, "TX02-test", "guest-1")
    call = http.request.call_args_list[4]
    assert call.args == (
        "PUT",
        GDO + "/api/v6.0/accounts/test-account/door_openers/TEST-GDO-1/OokTx/assign",
    )
    assert call.kwargs["json"] == {
        "name": "Remote",
        "tx_serial_number": "TX02-test",
        "is_delete_in_progress": False,
        "account_user_id": "guest-1",
    }


async def test_accessory_assignment_rejects_guest_without_opener_access(tokens, http, door):
    manager = make_management(tokens, http, door)
    http.request.side_effect = [
        json_response(accessory_payload(door)),
        json_response(access_payload()),
        json_response(guest_payload(allowed=[])),
    ]
    with pytest.raises(CommandRejected, match="no confirmed access"):
        await manager.assign_accessory(door, "TX02-test", "guest-1")
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


async def test_pin_role_change_before_write_is_rejected_even_with_grant(tokens, http):
    device = make_lock(allow_pin_code=True)
    manager = make_management(tokens, http, device)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response({"items": []}),
        json_response(access_payload("guest")),
    ]
    with pytest.raises(CommandRejected, match="permissions"):
        await manager.save_pin(device, name="Visitor", pin="123456")
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


async def test_pin_metadata_repr_never_contains_names_or_id(tokens, http):
    device = make_lock(allow_pin_code=True)
    manager = make_management(tokens, http, device)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response({"items": [pin_item(pin="123456")]}),
    ]
    metadata = await manager.pins(device)
    assert metadata[0].status == "synced"
    for secret in ("123456", "pin-1", "Visitor", "enabled"):
        assert secret not in repr(metadata)


async def test_schedule_can_be_recreated_in_empty_associated_configuration(
    tokens, http, camera_device
):
    manager = make_management(tokens, http, camera_device)
    http.request.side_effect = [
        json_response({"items": [{"id": "config-1", "schedules": []}]}),
        json_response(access_payload()),
        json_response({}),
        json_response(scene_payload()),
    ]
    await manager.save_schedule(
        camera_device,
        "privacy_mode",
        "config-1",
        label="Evening",
        days=["monday", "friday"],
        start_time="18:30:00",
        end_time="22:00:00",
        enabled=True,
    )
    assert http.request.call_args_list[2].args[0] == "POST"
    assert manager.configuration_ids(camera_device, "privacy_mode") == ("config-1",)


async def test_management_confirmation_network_loss_is_uncertain(tokens, http, door):
    manager = make_management(tokens, http, door)
    http.request.side_effect = [
        json_response(accessory_payload(door)),
        json_response(access_payload()),
        json_response({}),
        CannotConnect("Synthetic"),
        CannotConnect("Synthetic"),
    ]
    with pytest.raises(CommandUncertain, match="confirmation is unavailable"):
        await manager.rename_accessory(door, "TX02-test", "New")
    assert len([call for call in http.request.call_args_list if call.args[0] == "PUT"]) == 1


async def test_management_confirmation_survives_one_lost_read_back(tokens, http, door):
    """A single network failure while polling is not an outcome; keep confirming."""
    manager = make_management(tokens, http, door)
    http.request.side_effect = [
        json_response(accessory_payload(door)),
        json_response(access_payload()),
        json_response({}),
        CannotConnect("Synthetic"),
        json_response(accessory_payload(door, name="New")),
    ]
    await manager.rename_accessory(door, "TX02-test", "New")
    assert len([call for call in http.request.call_args_list if call.args[0] == "PUT"]) == 1


def modern_lock(**state):
    return make_lock(
        kind="wifilockgateway",
        automatic_lock_state="disabled",
        automatic_lock_delay_duration=30,
        door_sensor_state="enabled",
        door_sensor_calibration_status="calibrated",
        **state,
    )


@pytest.mark.parametrize(
    "seconds,body",
    [
        (0, {"automatic_lock_state": "Disable"}),
        (90, {"automatic_lock_state": "Enable", "automatic_lock_delay_duration": 90}),
    ],
)
async def test_modern_auto_lock_wire_contract_and_completion(tokens, http, seconds, body):
    device = modern_lock()
    manager = make_management(tokens, http, device)
    observed = replace(
        device,
        state={
            **device.state,
            "automatic_lock_state": "enabled" if seconds else "disabled",
            "automatic_lock_delay_duration": seconds or 30,
        },
    )
    manager.client.devices.side_effect = [[device], [observed]]
    http.request.side_effect = [json_response(access_payload()), json_response({})]
    await manager.set_auto_lock(device, seconds)
    call = http.request.call_args_list[1]
    assert call.args == (
        "PUT",
        "https://devices-action.myq-cloud.com/api/v1/accounts/test-account/"
        "devices/test-lock/lockconfiguration",
    )
    assert call.kwargs["json"] == body
    assert "params" not in call.kwargs


@pytest.mark.parametrize("seconds", [-1, 1, 31, 181, True, 30.0, "30", float("nan")])
async def test_modern_auto_lock_rejects_non_native_values_before_network(tokens, http, seconds):
    manager = make_management(tokens, http, modern_lock())
    with pytest.raises(CommandRejected):
        await manager.set_auto_lock(modern_lock(), seconds)
    http.request.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"device_type": "vendor_lock", "device_platform": "seam"},
        {"device_type": "lockitron"},
        {"state": {"online": True}},
        {"state": {"online": True, "automatic_lock_state": "requested"}},
    ],
)
async def test_modern_lock_requires_native_gateway_and_reported_setting(tokens, http, changes):
    device = replace(modern_lock(), **changes)
    manager = make_management(tokens, http, device)
    http.request.return_value = json_response(access_payload())
    with pytest.raises(CommandRejected):
        await manager.set_auto_lock(device, 60)
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


async def test_door_sensor_setting_does_not_send_auto_lock_fields(tokens, http):
    device = modern_lock()
    device = replace(device, state={**device.state, "door_sensor_calibration_status": "CALIBRATED"})
    manager = make_management(tokens, http, device)
    manager.client.devices.side_effect = [
        [device],
        [replace(device, state={**device.state, "door_sensor_state": "DISABLED"})],
    ]
    http.request.side_effect = [json_response(access_payload()), json_response({})]
    await manager.set_door_sensor(device, False)
    assert http.request.call_args_list[1].kwargs["json"] == {"door_sensor_state": "Disable"}


async def test_sensor_configuration_requires_existing_calibration(tokens, http):
    device = modern_lock()
    device = replace(
        device, state={**device.state, "door_sensor_calibration_status": "notcalibrated"}
    )
    manager = make_management(tokens, http, device)
    http.request.return_value = json_response(access_payload())
    with pytest.raises(CommandRejected, match="calibrated"):
        await manager.set_door_sensor(device, True)
    assert http.request.await_count == 1


def test_modern_setting_entities_show_off_and_current_sensor_state():
    device = modern_lock()
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            devices={device.key: device}, account_access=parse_account_access(access_payload())
        ),
        last_update_success=True,
        config_entry=None,
    )
    select = next(management_select_entities(coordinator, device))
    sensor = next(management_switch_entities(coordinator, device))
    assert select.current_option == "off" and select.available
    assert sensor.is_on is True and sensor.available
    coordinator.data.account_access = {}
    assert not select.available and not sensor.available


def workflow_details(feature):
    details = {"id": "CAM123", "cap": {"cam": {}}, "settings": {"cam": {"enabled": True}}}
    if feature == "privacy_mode":
        details["cap"]["cam"]["enableSwitch"] = True
    elif feature == "voice_deter":
        details["cap"]["cam"]["voiceDeter"] = True
    elif feature == "floodlight":
        details["cap"]["lightList"] = [
            {"type": "floodlight", "switchStateList": ["on", "off", "program"]}
        ]
    else:
        details["cap"]["cam"]["ptz"] = {"maxPresets": 4}
        details["settings"]["cam"]["ptz"] = {
            "presets": [{"id": 101, "index": 0}, {"id": 102, "index": 1}]
        }
    return details


@pytest.mark.parametrize("feature", ["privacy_mode", "patrol_mode", "voice_deter", "floodlight"])
async def test_first_workflow_configuration_exact_body_and_readback(
    tokens, http, camera_device, feature
):
    camera = replace(camera_device, device_type="smartptzcam1")
    manager = make_management(tokens, http, camera)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response(workflow_details(feature)),
        json_response({"items": []}),
        json_response(access_payload()),
        json_response({"configuration_id": "config-1"}),
        json_response(scene_payload()),
    ]
    await manager.create_schedule(
        camera,
        feature,
        label="Evening",
        days=["monday", "friday"],
        start_time="18:30:00",
        end_time="22:00:00",
        enabled=True,
    )
    call = http.request.call_args_list[4]
    assert call.args == (
        "POST",
        "https://workflows.myq-cloud.com/api/v1.0/accounts/test-account/workflows/scenes",
    )
    assert call.kwargs["json"] == {
        "enabled": True,
        "action": {
            "devices": [{"enabled": True, "serial_number": camera.serial, "feature": feature}]
        },
        "schedules": [
            {
                "label": "Evening",
                "enabled": True,
                "days": "monday, friday",
                "start_time": "18:30:00",
                "end_time": "22:00:00",
            }
        ],
    }
    assert manager.configuration_ids(camera, feature) == ("config-1",)


@pytest.mark.parametrize(
    "presets",
    [[], [{"id": 1}], [{"id": 1}, {"id": 1}], [{"id": True}, {"id": 2}], [{"id": 1}, {"id": "2"}]],
)
def test_first_patrol_schedule_requires_distinct_native_presets(camera_device, presets):
    camera = replace(camera_device, device_type="smartptzcam1")
    details = workflow_details("patrol_mode")
    details["settings"]["cam"]["ptz"]["presets"] = presets
    assert not can_create_workflow(camera, "patrol_mode", details)


async def test_first_workflow_rejects_existing_configuration_without_mutating(
    tokens, http, camera_device
):
    camera = replace(camera_device, device_type="smartptzcam1")
    manager = make_management(tokens, http, camera)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response(workflow_details("privacy_mode")),
        json_response({"items": [{"id": "existing", "schedules": []}]}),
    ]
    with pytest.raises(CommandRejected, match="existing workflow"):
        await manager.create_schedule(
            camera,
            "privacy_mode",
            label="Evening",
            days=["monday"],
            start_time="18:30:00",
            end_time="22:00:00",
            enabled=True,
        )
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


async def test_first_workflow_unknown_confirmation_never_replays(tokens, http, camera_device):
    camera = replace(camera_device, device_type="smartptzcam1")
    manager = make_management(tokens, http, camera)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response(workflow_details("privacy_mode")),
        json_response({"items": []}),
        json_response(access_payload()),
        json_response({"id": "config-1"}),
        json_response({"items": []}),
        json_response({"items": []}),
    ]
    with pytest.raises(CommandUncertain):
        await manager.create_schedule(
            camera,
            "privacy_mode",
            label="Evening",
            days=["monday"],
            start_time="18:30:00",
            end_time="22:00:00",
            enabled=True,
        )
    assert len([call for call in http.request.call_args_list if call.args[0] == "POST"]) == 1


async def test_pin_delete_stops_when_myq_reports_failure(tokens, http):
    # l31/d stops on a failed status instead of waiting out the confirmation window.
    device = make_lock(allow_pin_code=True)
    manager = make_management(tokens, http, device)
    http.request.side_effect = [
        json_response(access_payload()),
        json_response({"items": [pin_item()]}),
        json_response(access_payload()),
        json_response({}),
        json_response({"items": [pin_item("failed")]}),
    ]
    with pytest.raises(CommandRejected, match="deletion failed"):
        await manager.delete_pin(device, "pin-1")


async def test_accessory_rename_follows_the_apps_limits(tokens, http, door):
    manager = make_management(tokens, http, door)
    with pytest.raises(CommandRejected, match="24"):
        await manager.rename_accessory(door, "TX02-test", "x" * 25)
    http.request.assert_not_awaited()
    inventory = accessory_payload(door)
    inventory["gdo_transmitter_names"].append(
        {**inventory["gdo_transmitter_names"][0], "name": "Keypad", "tx_serial_number": "TX02-k"}
    )
    http.request.return_value = json_response(inventory)
    # s50/e1.c0: another accessory on the opener already uses the name, ignoring case.
    with pytest.raises(CommandRejected, match="already has that name"):
        await manager.rename_accessory(door, "TX02-test", "keypad")
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


async def test_accessory_inventory_reads_like_the_app(tokens, http, door):
    # yz0/g.a: null list empty, missing flag false, missing name empty, serial unchecked.
    manager = make_management(tokens, http, door)
    http.request.return_value = json_response(
        {
            "gdo_transmitter_names": [
                {"tx_serial_number": "TX02-a", "transmitter_type": "OokTx"},
                {"name": "No serial"},
                {"tx_serial_number": "TX02-a", "name": "Duplicate"},
                "not an object",
            ]
        }
    )
    (accessory,) = await manager.accessories(door)
    assert (accessory.serial, accessory.name, accessory.deleting) == ("TX02-a", "", False)
    http.request.return_value = json_response(
        {"gdo_serial_number": door.serial, "gdo_transmitter_names": None}
    )
    assert await manager.accessories(door) == ()


def test_schedule_overlap_matches_the_app():
    def existing(days, start, end):
        return WorkflowSchedule("c", "s", "privacy_mode", "", True, days, start, end)

    def new(days, start, end):
        return {"days": days, "start_time": start, "end_time": end}

    evening = existing("monday, friday", "18:30:00", "22:00:00")
    assert _overlaps(new("friday", "21:00:00", "23:00:00"), evening)
    assert not _overlaps(new("friday", "22:00:00", "23:00:00"), evening)  # Touching only.
    assert not _overlaps(new("tuesday", "18:30:00", "22:00:00"), evening)
    # An end not after its start runs past midnight, including Sunday into Monday.
    overnight = existing("sunday", "23:00:00", "01:00:00")
    assert _overlaps(new("monday", "00:30:00", "02:00:00"), overnight)
    assert not _overlaps(new("monday", "01:00:00", "02:00:00"), overnight)


async def test_enabling_an_overlapping_schedule_sends_nothing(tokens, http, camera_device):
    manager = make_management(tokens, http, camera_device)
    scenes = scene_payload()
    scenes["items"][0]["schedules"].append(
        {
            "id": "schedule-2",
            "label": "Late",
            "enabled": False,
            "days": "friday",
            "start_time": "21:00:00",
            "end_time": "23:00:00",
        }
    )
    http.request.return_value = json_response(scenes)
    with pytest.raises(CommandRejected, match="overlaps"):
        await manager.set_schedule_enabled(
            camera_device, "privacy_mode", "config-1", "schedule-2", True
        )
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


async def test_schedule_read_defaults_missing_fields_like_the_app(tokens, http, camera_device):
    manager = make_management(tokens, http, camera_device)
    http.request.return_value = json_response(
        {"items": [{"configuration_id": "config-1", "schedules": [{"id": "schedule-1"}]}]}
    )
    (schedule,) = await manager.schedules(camera_device, "privacy_mode")
    assert (schedule.enabled, schedule.days, schedule.label) == (False, "", "")


async def test_guests_can_only_be_given_devices_the_app_offers(tokens, http, door_payload):
    # iv0/c.b: a shareable type (or a camera) and a serial with a known marker.
    opener = Device.parse("test-account", {**door_payload, "serial_number": "CG0812345"})
    thermostat = Device.parse(
        "test-account",
        {**door_payload, "serial_number": "CG0899999", "device_type": "vendor_thermostat"},
    )
    assert shareable(opener) and not shareable(thermostat)
    manager = make_management(tokens, http, opener)
    manager.client.devices = AsyncMock(return_value=[opener, thermostat])
    http.request.side_effect = [
        json_response(access_payload()),
        json_response(guest_payload(allowed=[])),
    ]
    with pytest.raises(CommandRejected, match="does not offer"):
        await manager.set_guest_devices(
            opener, "guest-1", "access-1", [opener.serial, thermostat.serial]
        )
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)

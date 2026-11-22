"""Gateway merge diagnostics expose relationship outcomes without device identities."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.myq.api.models import Device
from custom_components.myq.coordinator import Snapshot
from custom_components.myq.diagnostics import async_get_config_entry_diagnostics


def pair():
    opener = Device.parse(
        "private-account",
        {
            "serial_number": "private-opener-serial",
            "name": "Private opener name",
            "device_type": "wifigaragedooropener",
            "parent_device_id": "private-gateway-serial",
        },
    )
    gateway = Device.parse(
        opener.account_id,
        {
            "serial_number": "private-gateway-serial",
            "name": "Private gateway name",
            "device_type": "wifigdogateway",
        },
    )
    return opener, gateway


@pytest.mark.parametrize("status", ["merged", "registry_blocked", "no_unique_match"])
@pytest.mark.parametrize("gateway_first", [False, True])
async def test_gateway_status_reports_only_anonymous_relationships(status, gateway_first):
    opener, gateway = pair()
    if status == "no_unique_match":
        opener = replace(opener, parent_device_id=None)
    ordered = (gateway, opener) if gateway_first else (opener, gateway)
    coordinator = SimpleNamespace(
        last_update_success=True,
        data=Snapshot({device.key: device for device in ordered}),
        gateway_openers={gateway.key: opener.key} if status == "merged" else {},
    )
    data = await async_get_config_entry_diagnostics(None, SimpleNamespace(runtime_data=coordinator))
    rows = data["devices"]
    gateway_row = rows[0 if gateway_first else 1]
    opener_row = rows[1 if gateway_first else 0]
    assert gateway_row["merge_status"] == status
    assert gateway_row["opener_index"] == (
        None if status == "no_unique_match" else (1 if gateway_first else 0)
    )
    assert opener_row["parent_link_fields"] == (
        [] if status == "no_unique_match" else ["parent_device_id"]
    )
    assert "merge_status" not in opener_row
    assert "opener_index" not in opener_row
    for private in (opener.serial, gateway.serial, opener.account_id, opener.name, gateway.name):
        assert private not in json.dumps(data)
        assert private not in repr(opener)


async def test_parent_link_field_names_are_normalized_and_never_include_values():
    opener, gateway = pair()
    opener = replace(
        opener,
        gateway_id=gateway.serial,
        state={"parent_serial_number": gateway.serial, "gatewayID": gateway.serial},
    )
    coordinator = SimpleNamespace(
        last_update_success=True,
        data=Snapshot({opener.key: opener, gateway.key: gateway}),
    )
    data = await async_get_config_entry_diagnostics(None, SimpleNamespace(runtime_data=coordinator))
    assert data["devices"][0]["parent_link_fields"] == [
        "gateway_id",
        "parent_device_id",
        "parent_serial_number",
    ]
    assert data["devices"][1]["merge_status"] == "registry_blocked"
    assert gateway.serial not in json.dumps(data)

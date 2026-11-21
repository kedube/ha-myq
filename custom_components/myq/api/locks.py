"""Cloud lock commands traced through DeviceActuator and LockServiceV6Api."""

from urllib.parse import quote

from .client import Client
from .const import ACTIONS, LOCKS
from .errors import CommandRejected
from .models import Device


def is_lock(device: Device) -> bool:
    """Match DeviceFactory's lock types, including vendor-prefixed Seam locks."""
    return device.device_type in {"lockitron", "wifilockgateway"} or device.device_type.endswith(
        "_lock"
    )


def lock_state(device: Device | None) -> str | None:
    """Decode LockDevice's cloud state without using stale Bluetooth state."""
    if device is None or not is_lock(device):
        return None
    value = device.state.get("lock_state")
    if not isinstance(value, str):
        return None
    value = value.lower()
    return value if value in {"locked", "unlocked", "locking", "unlocking"} else None


def uses_action_api(device: Device) -> bool:
    """DeviceActuator routes Wi-Fi lock gateways and Seam through DeviceAction."""
    return device.device_type == "wifilockgateway" or device.device_platform == "seam"


def has_remote_path(device: Device) -> bool:
    """A cloud route exists at all; Bluetooth-only locks (no gateway) have none."""
    return is_lock(device) and (
        uses_action_api(device) or (isinstance(device.gateway_id, str) and bool(device.gateway_id))
    )


def has_remote_control(device: Device) -> bool:
    """BluetoothLeDevice.R1: never request a Bluetooth-only command from HA."""
    return is_lock(device) and (
        uses_action_api(device)
        or (
            isinstance(device.gateway_id, str)
            and bool(device.gateway_id)
            and device.state.get("bluetooth_connectability") in ("remote", "busy")
        )
    )


def lock_online(device: Device) -> bool:
    """Device.online already applies LockDevice's Wi-Fi gateway camera rule."""
    return device.online


def can_control(device: Device) -> bool:
    """Honor native monitor/unattended restrictions and confirmed remote state."""
    return (
        has_remote_control(device)
        and lock_online(device)
        # Monitor-only, PIN lockout and work-light error, as in the app's precheck.
        and device.control_blocked() is None
        # Device.X0 uses A1, whose default is true.
        and device.state.get("is_unattended_close_allowed", True) is True
        and lock_state(device) in ("locked", "unlocked")
    )


class Locks:
    """Keep lock writes on the shared, non-replaying device mutation path."""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def command(self, device: Device | None, action: str) -> None:
        if device is None or action not in ("lock", "unlock") or not can_control(device):
            reason = device.control_blocked() if device is not None else None
            raise CommandRejected(reason or "MyQ lock does not currently permit remote control")
        account = quote(device.account_id, safe="")
        serial = quote(device.serial, safe="")
        if uses_action_api(device):
            # DeviceActionRepository omits the nullable trigger/device_family fields.
            await self.client.device_mutation(
                device,
                "PUT",
                ACTIONS,
                f"/api/v1.0/accounts/{account}/devices/{serial}",
                json={"action": action},
            )
        else:
            # requestRemote=false starts a separate encrypted Bluetooth operation.
            # This integration implements only the confirmed gateway/cloud path.
            await self.client.device_mutation(
                device,
                "POST",
                LOCKS,
                f"/api/v6.0/accounts/{account}/devices/locks/{serial}/{action}",
                params={"requestRemote": "true"},
            )

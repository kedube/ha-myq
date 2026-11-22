"""Resolve only returned same-account relationships from DeviceAttributes."""

from .models import Device


def wifi_gdo_pairs(inventory: dict[str, Device]) -> dict[str, Device]:
    """Match a built-in Wi-Fi gateway to its one explicitly associated opener."""

    def parent(device: Device) -> str | None:
        value = device.gateway_id or device.state.get("parent_serial_number")
        return value if isinstance(value, str) and value else None

    pairs: dict[str, Device] = {}
    for gateway in inventory.values():
        if gateway.device_type != "wifigdogateway":
            continue
        serials = {
            device.serial
            for device in inventory.values()
            if device.is_door
            and device.account_id == gateway.account_id
            and parent(device) == gateway.serial
        }
        reported = gateway.state.get("associated_gdo_serial_numbers")
        if reported is not None:
            if not isinstance(reported, list) or any(
                not isinstance(serial, str) or not serial for serial in reported
            ):
                continue
            serials.update(reported)
        if len(serials) != 1:
            continue
        opener = inventory.get(f"{gateway.account_id}:{next(iter(serials))}")
        if (
            opener is not None
            and opener.account_id == gateway.account_id
            and opener.device_type == "wifigaragedooropener"
            and parent(opener) in {None, gateway.serial}
        ):
            pairs[gateway.key] = opener
    # Conflicting reverse links must not combine two gateways with one opener.
    return {
        key: opener
        for key, opener in pairs.items()
        if sum(other.key == opener.key for other in pairs.values()) == 1
    }


def associations(device: Device, inventory: dict[str, Device]) -> dict[str, tuple[Device, ...]]:
    """Unknown, cross-account and self references never create synthetic devices."""

    def resolve(serial, predicate):
        if not isinstance(serial, str) or not serial or serial == device.serial:
            return None
        candidate = inventory.get(f"{device.account_id}:{serial}")
        return (
            candidate
            if candidate is not None
            and candidate.account_id == device.account_id
            and predicate(candidate)
            else None
        )

    gateway = resolve(device.gateway_id, lambda item: True)
    # attached_camera_serial_number links an opener to its built-in camera (u.Q1).
    camera = resolve(
        device.state.get("associated_camera_serial_number"), lambda item: item.is_camera
    ) or resolve(device.state.get("attached_camera_serial_number"), lambda item: item.is_camera)
    raw_gdos = device.state.get("associated_gdo_serial_numbers")
    doors = {}
    if isinstance(raw_gdos, list) and all(isinstance(value, str) for value in raw_gdos):
        for serial in raw_gdos:
            if linked := resolve(serial, lambda item: item.is_door):
                doors[linked.key] = linked
    return {
        "gateway": (gateway,) if gateway is not None else (),
        "cameras": (camera,) if camera is not None else (),
        "garage_doors": tuple(doors.values()),
    }


def linked_camera(device: Device, inventory: dict[str, Device]) -> Device | None:
    """The camera record of an opener (attached or associated camera serial)."""
    cameras = associations(device, inventory)["cameras"]
    return cameras[0] if cameras else None


def camera_opener(camera: Device, inventory: dict[str, Device]) -> Device | None:
    """The opener whose built-in or associated camera is this camera record."""
    for candidate in inventory.values():
        if (
            candidate.is_door
            and candidate.account_id == camera.account_id
            and candidate.state.get("has_camera") is not False
            and linked_camera(candidate, inventory) is camera
        ):
            return candidate
    return None

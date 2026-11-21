"""Resolve only returned same-account relationships from DeviceAttributes."""

from .models import Device


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

"""Allowlisted diagnostics exclude identifiers, tokens, media, and arbitrary payloads."""

from .api.associations import wifi_gdo_pairs
from .camera import _live_route


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    devices = coordinator.data.devices
    positions = {key: index for index, key in enumerate(devices)}
    pairs = wifi_gdo_pairs(devices)
    merged = getattr(coordinator, "gateway_openers", {})
    monitor = getattr(coordinator, "status_monitor", None)

    def live_video(device):
        if not device.is_camera:
            return {}
        source = getattr(coordinator, "video_sources", {}).get(device.key)
        return {
            # "legacy": a TC camera served as a stream source; "kvs": MyQ WebRTC.
            "live_video_route": _live_route(device, coordinator.data.cameras.get(device.key, {})),
            "live_video_source": source.status() if source is not None else None,
            "live_view_open": device.key in getattr(coordinator, "live_sessions", {})
            or (source is not None and source.session is not None),
        }

    def gateway_status(device):
        if device.device_type != "wifigdogateway":
            return {}
        opener = pairs.get(device.key)
        status = "no_unique_match"
        if opener is not None:
            status = "merged" if merged.get(device.key) == opener.key else "registry_blocked"
        return {
            "opener_index": positions[opener.key] if opener is not None else None,
            "merge_status": status,
        }

    return {
        "last_update_success": coordinator.last_update_success,
        "account_access_error": coordinator.data.account_access_error,
        # The idle Tend connection uses the same login as live video.
        "camera_status_connected": getattr(monitor, "connected", None),
        "camera_status_error": getattr(monitor, "last_error", None),
        "devices": [
            {
                "device_type": device.device_type,
                "family": device.family,
                "online": device.online,
                "door_state": device.door_state,
                "state_fields": sorted(device.state),
                "parent_link_fields": [
                    name
                    for name, value in (
                        ("gateway_id", device.gateway_id),
                        ("parent_device_id", device.parent_device_id),
                        ("parent_serial_number", device.state.get("parent_serial_number")),
                    )
                    if isinstance(value, str) and value
                ],
                **gateway_status(device),
                **live_video(device),
                "camera_details_available": device.key in coordinator.data.cameras,
                "camera_error": coordinator.data.camera_errors.get(device.key),
                "garage_settings_error": coordinator.data.garage_errors.get(device.key),
                # Counts and error names only: history and alert content name people.
                "history_events": len(coordinator.data.history.get(device.key, ())),
                "history_error": coordinator.data.history_errors.get(device.account_id),
                "alert_rules": len(coordinator.data.alerts.get(device.key, ())),
                "alert_error": coordinator.data.alert_errors.get(device.account_id),
                "account_role": (
                    access.role
                    if (access := coordinator.data.account_access.get(device.account_id))
                    else "unknown"
                ),
            }
            for device in devices.values()
        ],
    }

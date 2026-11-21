"""Allowlisted diagnostics exclude identifiers, tokens, media, and arbitrary payloads."""


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {
        "last_update_success": coordinator.last_update_success,
        "account_access_error": coordinator.data.account_access_error,
        "devices": [
            {
                "device_type": device.device_type,
                "family": device.family,
                "online": device.online,
                "door_state": device.door_state,
                "state_fields": sorted(device.state),
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
            for device in coordinator.data.devices.values()
        ],
    }

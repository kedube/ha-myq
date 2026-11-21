# Actions

The integration adds actions under `myq` (Developer Tools → Actions)
for account management and reading data the entities don't show. Camera
features are described in [cameras](cameras.md).

## Management actions

These actions require a Home Assistant administrator. Actions that target a
device take its Home Assistant **device ID**. Each change checks your current
MyQ account role (as the app's screens do), is sent once, and waits until MyQ
reports it complete. A
pending, lost or unconfirmed result is reported, not retried.

| Action | Use |
| --- | --- |
| `get_accessories` | Return paired accessory metadata and enable battery diagnostics for this loaded integration; refresh tracked inventories every five minutes. |
| `rename_accessory`, `remove_accessory`, `assign_accessory` | Manage an existing paired accessory; assignment requires an existing guest who already has opener access. |
| `set_auto_relock` | Set a supported legacy remote lock timer. Eligible locks also expose a switch and delay selector. |
| `set_auto_lock` | Set a native Wi-Fi lock's automatic lock delay (30, 60, 90, 120 or 180 seconds), or zero to disable it. Eligible devices expose a delay selector. |
| `set_door_sensor` | Enable/disable an already configured and calibrated native Wi-Fi lock sensor. Eligible devices expose a switch. |
| `get_schedules` | Return schedules and configuration IDs for a known camera feature, including empty existing configurations. |
| `save_schedule`, `set_schedule_enabled`, `delete_schedule` | Manage schedules belonging to that device and feature. Omit the configuration ID only to create the first supported feature configuration when none exists. Times use `HH:mm:00`; weekdays use lowercase names. Toggling sends all five schedule fields, as the app does. |
| `get_pins`, `save_pin`, `delete_pin` | Manage legacy remote lock PIN metadata and require `synced` status after a write. |
| `get_guests`, `set_guest_devices`, `remove_guest` | Inspect existing guests, replace a guest schedule's allowed-device list while preserving its time restrictions, or revoke a guest. Only device types the app offers to guests can be added. Unlike the app, neither action deletes the guest's keypad PINs; remove those in the MyQ app first. |
| `rename_device` | Rename a device in MyQ (1 to 24 characters, account owner only). The route follows the app's family dispatch: lamps, door openers and gates, hubs, or the generic devices route. |
| `get_device_history` | Return one page of the device's activity history, oldest first, with a continuation value for the next page. User identifiers are left out. Not available to guests, as in the app. |
| `get_alerts` | Return the device's notification rules and whether each is enabled. |
| `snooze_alerts`, `cancel_alert_snooze` | Snooze all of the account's MyQ notifications for 1 minute to 7 days, or end the snooze. Account-wide, as in the app. Owners and co-owners only, as in the app. |
| `get_thermostat_programs` | Return a linked thermostat's presets and schedules (read only; the app has no working preset activation). |
| `validate_session` | Verify identity, accounts and devices; optionally test token renewal and persistence. |

Related entities, offered to the roles the app offers them to:

- **Disable remotes** switch (opener lock mode), for the owner of a Wi-Fi or 3C opener.
- **Clear PIN lockout** button, for owners and co-owners, disabled by default;
  available only while a wrong-PIN keypad lockout is active.
- **Alert: …** switches, one per notification rule.
- **Activity** event entity per door, lamp and lock, disabled by default. While
  one is enabled, the integration reads one history page per account on each full
  poll and fires an event for each new entry (`event_type` is the device event,
  such as `open` or `locked`; `activity` is the history kind). For an opener with a
  camera, a door open or close also carries `recording`: the media-source ID of the
  door-triggered clip, matched as the app's history screen does. This costs one clip
  read per such opener per full poll. `get_device_history` adds the same field.

Use IDs returned by the corresponding `get_*` action. These actions return data
through HA's action response mechanism. Accessory battery entities are disabled
by default and can be enabled in the entity registry. They start refreshing after
`get_accessories`; after a restart, refreshing resumes for openers that still have an
enabled accessory battery entity.

Device-type diagnostic sensors include the native account role and HA device IDs
for returned same-account camera, garage-door and gateway associations. An
unknown role or missing relationship does not create permissions or synthetic
devices.

### PIN file input

`save_pin` accepts `pin_file`, a filename directly in the HA configuration
directory. Create a private file owned by HA's operating-system user, for example:

```json
{"pin":"123456"}
```

Use your own valid PIN. Set file permissions to `0600` (owner only). Supply the
filename, display name, selected device, and optionally the existing `pin_id`.
The integration rejects symlinks, nonregular files and publicly readable files.
Only the filename enters HA's service events and automation traces; the PIN is
read privately, sent once, and excluded from state, responses and integration
diagnostics. Remove the file after use; HA backups may include files you leave
in its configuration directory.

### Session validation

Call `myq.validate_session` with the configuration entry ID to check
that a session works. It reads the profile, accounts, devices and permissions.
With `refresh: true` it also renews the session, saves the renewed tokens and
confirms the account is unchanged. The result contains counts and roles, never
credentials or identifiers, and it never operates a device. It cannot create a
session; see [sign-in](sign-in.md) for that.

## Not available as actions

Device pairing and setup, new guest invitations and role changes, and
Bluetooth-only lock operations are not offered. For the supported lock variants,
roles, accessory types and schedule features, see the
[management reference](api/management.md).

# App parity review (0.1.10)

This page records a review of every device family against MyQ Android
5.324.0.1132442, and the changes that followed. Paths are jadx paths;
`model/` means `com/chamberlain/myq/sdk/devices/services/external/feature/common/model/`.
Everything here comes from static analysis with synthetic tests. None of it has
been checked against a live account.

## Control checks (the app's precheck)

`com/chamberlain/myq/features/places/view/b.java:C` runs before any tap acts on a
device. The integration now applies the same checks, in the same order, to doors,
lamps and locks (`Device.control_blocked`):

| Check | App source | Effect in HA |
| --- | --- | --- |
| Virtual opener sensor | `model/devices/l0.v1`: `sensor_comm_error` with `number_of_learned_dps_devices > 0` | No open/close |
| Monitor-only | `k.m1` (default false; true for virtual openers) | No commands |
| PIN lockout | `k.t0`: `failed_credential_lockout_active` | No commands |
| Work-light error | `k.N0`: `attached_work_light_error_present` | No commands |

Defaults now match the app:

- **Connectivity.** `k.p1` reads a missing `online` as true. A Wi-Fi lock gateway
  uses `camera_online` whenever it is non-null (`f0.h2`). An explicit non-Boolean
  value is still treated as offline.
- **Unattended flags.** A missing close flag reads as allowed (`k.A1`). The app
  never reads the open flag, so a missing one also reads as allowed. An explicit
  false still denies that direction.

**Region-specific behavior.** When `is_unattended_close_allowed` is false, the app
blocks *all* control only for international product-line accounts. The check is
`n1 && h0.o()`, where `h0.o` is `isInternationalProductLineUserForMonitorOnly`, a
country check. In the US and Canada it blocks closing only (`i50/g0.j`). HA cannot
see the account's country, so it keeps the US behavior.

## Statistics and polled fields (no extra requests)

- **State classes.** Door cycles are `total_increasing`; battery levels and camera
  Wi-Fi strength are `measurement`.
- **Opener health.**
  - **Last state change:** `last_update`, or `last_status` while offline. It is
    parsed as `yyyy-MM-dd'T'HH:mm:ss` UTC, as `ct0/a` does.
  - **Fault codes:** `active_fault_codes`.
  - **Backup battery:** an enum with a problem sensor (`xt/a.java`).
  - **Work light not paired.**
  - **Safety sensor alignment:** read from `is_unattended_close_allowed`, as the
    health screen does (`xt/k1`).
- **Door sensor fault.** It counts only while a sensor is learned
  (`GdoDeviceUiItem`).
- **Locks.**
  - **Low battery:** the field is `low_battery` (`yc1/DeviceAttributes:294`); the
    earlier `lock_low_battery` key never matched.
  - **PIN lockout:** state, plus attempts and duration (disabled by default).
  - **Orientation and calibration failure.**
  - **Door contact:** a binary sensor, using the app's banner rule.
  - **Firmware:** an update entity from `firmware_available` and
    `latest_available_firmware_version`.
  - **Device registry model:** taken from `lock_model`.
- **Cameras.** These come from the details response that is already polled:
  - battery (`status.pwr.battLv`)
  - power source (`pwrSrc`)
  - charging (`battChrgMw > 0`)
  - Wi-Fi strength (`net.eth.wifiStrength`)
  - firmware (`hw.firmwareVer`, `hw.firmwareUpgradeAvail`, `status.upgradeInProgress`)
  - MAC address (`net.eth.mac`), added as a device connection
- **Offline values.** Battery values, the offline reason and the last state change
  stay available while a device is offline.

## Cameras

- **Power state.** `VideoDeviceInfo.c()` reads a missing `sleepMode` as available
  unless the camera sleeps (sleep interval above zero, or a battery or video-keypad
  model when no power data is reported). An unrecognized `sleepMode` is UNKNOWN,
  which is not available, so the camera is woken. A missing `status.online` reads as
  offline. Neither raises an error any more. `sleepMode` is case-insensitive.
- **Wake before writing.** Settings writes wake a sleeping camera first:
  `requiresHardwareDeviceOnline` defaults to true, and `mz0/c` wakes a camera that
  is not AVAILABLE.
- **Siren volume.** It requires `cap.siren.activatable` and uses the app's steps of
  60/70/80/90/100 (`y50/c`, `k80/l`, `xr0/DeviceSettings`).
- **Status LED.** It is read without regard to case (`i41/p0`).
- **New settings.**
  - **Wake on motion:** `{"wakeOnMotion": bool}`, gated on `cap.wakeOnMotionCap`.
    It is offered only when no power saver modes are advertised; the app shows this
    as its inverted "Battery saver" switch.
  - **Power saver:** `{"pwr":{"powerSaverMode":"AUTO|ON|OFF"}}`, with options from
    `cap.pwr.powerSaverModes`.
  - **Chime type:** mechanical (duration 0) or digital (duration 5), from
    `hb0/m.java`. The current enabled state is kept.
- **Snapshots.** These follow `wy/o.java`; see [cameras.md](../cameras.md).

## Thermostats

- **Enum case.** Enums use Gson's default adapter (`DeviceDeserializer` and
  `EnumTypeAdapter`), so the app reads names such as `HEAT_COOL`. HA now reads them
  without regard to case and still writes lowercase.
- **Defaults.** A missing unit means Fahrenheit; a missing `is_managed` means
  managed.
- **Setpoint range.** `temperature_threshold` no longer narrows the range; it is an
  alert band, shown as an attribute.
- **Running state.** `hvac_action` follows `HvacDevice.j2`.
- **Programs.** `get_thermostat_programs` reads presets and schedules:
  GET v1.0 `.../devices/thermostats/{serial}/presets` and `/schedules` on
  `myq-partner-devices` (`h01/a`).

## Management

| Feature | Wire | Gate |
| --- | --- | --- |
| Disable remotes (lock mode) | PUT `account-devices-gdo` v6.0 `door_openers/{s}/lockmode` `{"enable_lock_mode":bool}`; readback `in_vacation_mode` | Owner; Wi-Fi opener or 3C serial (`y80/o`, `iv0/c.k`) |
| Clear PIN lockout | POST `residential-credentials` v1.0 `devices/{target}/lockout/reset` `{"credential_type":"pin"}` | Owner + devices Manage; lockout active; button disabled by default |
| Rename | PUT `{"name"}` to lamps, door_openers, gateways or the generic `Devices` route (`yy0/k.J2`) | Owner; 1–24 characters (`y80/o`) |
| Alert rules | GET/PUT `alerts` v6.1 `alerts/{id}` with the complete AlertDto (`fo0/a`, `ho0/a`) | alerts View to read, Manage to write |
| Snooze | POST/DELETE `alerts` v6.0 `alerts/snooze` `{created_at, expires_at}` as `yyyy-MM-dd'T'HH:mm:ss.SSS'Z'` | alerts Manage |
| Device history | GET `accounthistory` v6.1 `deviceHistory?devices=…&continue=` (`h61/d`) | history View; read only |
| Schedule toggle | PUT with all five fields, as `l60/k2` → `c21/f` does | Unchanged |

- **Lockout target.** The reset goes to the first available serial in `Device.s()`
  (`yc1/f.java`): the gateway, then `parent_serial_number`, then the device itself.
- **Owner-only opener settings.** Opener settings and accessory writes are now
  owner-only, as `GdoDeviceSettingsProvider` shows them. Non-owners cost no
  settings reads.
- **Never called.** These routes are never called: the account-wide history
  DELETE, alert create/delete, firmware install, auto-secure, geofence, pairing
  and provisioning.

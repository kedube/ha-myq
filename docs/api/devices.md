# Devices

Accounts, the device model, device families and their control paths in MyQ
Android **5.324.0.1132442**: doors, gates, lamps, opener settings, locks and
thermostats, plus state confirmation and account roles. These findings are
static; no device operation has been confirmed against a live account.
"Implemented" notes describe the integration. Cameras are covered in
[cameras and video](video.md); what users get is in the
[devices guide](../devices.md).

## Accounts, discovery and data model

The application separates the signed-in profile, accounts accessible to that
profile, and devices within each account. These identities are not interchangeable.
The recovered account/device read paths preserve their literal route casing:

| Method and URL | Response facts | Static sources |
| --- | --- | --- |
| `GET https://profiles.myq-cloud.com/api/v6.0/my/profile` | Profile with `user_id`, `email` | `b91/d.java`, `a91/t.java`, `c91/f.java` |
| `GET https://accounts.myq-cloud.com/api/v6.0/accounts` | `accounts` array; account `id`, `name` | `an0/b.java`, `zm0/f.java`, `bn0/Account.java`, `bn0/AccountsListDto.java` |
| `GET https://accounts.myq-cloud.com/api/v6.0/accountAccess` | Account-scoped role and explicit resource/permission sets | `an0/a.java`, `zm0/b.java`, `bn0/AccountAccessEntry.java`, `cn0/a.java` |
| `GET https://devices.myq-cloud.com/api/v6.0/Accounts/{accountId}/Devices` | `items`, `count`, `href` | `gz0/a.java`, `hz0/b.java`, `yc1/DeviceList.java` |

Native `wy0/i.java` selects device API v6.0/v6.1/v6.2 using feature flags.
The integration currently uses v6.0 and iterates all returned accounts
sequentially. It rejects a response whose `count` exceeds the returned item
count instead of silently treating a partial list as complete. This is a local
completeness check; `DeviceList` did not establish a pagination protocol.

`yc1/f.java`, `yc1/DeviceAttributes.java`, `yc1/Specifications.java` and the
device factory define the principal data groups:

| Field/group | Purpose and interpretation |
| --- | --- |
| `serial_number`, `name`, `device_model` | Device identity/display; model may also come from `state.model_number` |
| `device_type`, `device_family` | Choose a device model and action path; model names alone do not establish commands |
| `capabilities` | Device capability strings; their mere presence does not resolve all settings/permission gates |
| `state` | Connectivity, actuator state, diagnostics, permissions, associations and family-specific readings |
| `specifications` | Advertised thermostat modes, bounds, thresholds and other specifications |
| `device_platform` | Affects dispatch, notably the `seam` partner-lock path |
| `gateway_id` | Top-level gateway association; native getter falls back to `state.gatewayID` |

The integration keys a device by **account ID + serial**, allowing identical
serial strings in different account contexts to remain distinct. It normalizes
type/family/platform names, validates the shapes it consumes, and treats absent
nullable state/capability/specification groups as empty rather than granting
actions. Normal discovery adds new HA entities on later polls; removed records
make existing entities unavailable.

Associations need care. An opener's camera reference is not itself a camera
record. Camera REST IDs and signaling aliases are different identifiers.
`wifilockgateway` resolves to a lock despite its name; a `smartlockcam` is its
camera component, not a lock action target. Remotes and wall controls may be
listed as opener accessories rather than independent cloud actuators.

## Native taxonomy and action dispatch

In source references below, `model/` abbreviates
`com/chamberlain/myq/sdk/devices/services/external/feature/common/model/`.
`model/o.java`, `g.java` and `f.java` define DeviceType, DeviceFamily and
DeviceFactory. Enum reconstruction has warnings; readable model/service call
paths corroborate the implemented families. Vendor `_lock` and `_thermostat`
recognition is suffix matching, confirmed by `p039kotlin/text/s.java`.

| Device family/types | Established control path | Integration boundary |
| --- | --- | --- |
| Residential, virtual and commercial openers; gates | Native garage-door model and action service | Permitted open/close; optional settings only for applicable residential openers |
| `lamp` | Lamp-specific account service | On/off only |
| `wifilockgateway`, Seam locks | Device action service | Lock/unlock with remote/state restrictions |
| Legacy `lockitron` / vendor `*_lock` | Locks v6 remote gateway or separate local Bluetooth operation | Cloud gateway branch only |
| Vendor `*_thermostat`, family `hvacs` | HVAC actions through the action service | Advertised modes/fan/setpoints with reported limits |
| Camera/Tend types | Tend REST and live signaling | Separate camera protocol; see [video](video.md) |
| Gateways, bridges, generic accessories, pet doors and unknowns | Several models/enums and management declarations | Returned diagnostics; no guessed actuation path |

The action service is
`https://devices-action.myq-cloud.com/api/v1.0/accounts/{account}/devices/{serial}`.
The **same PUT route has different typed bodies**. `az0/DeviceActionRequestBody.java`
is not evidence that every field is required for every action. `zy0/a.java:a`
selects family-specific fields, `bz0/e.java:a` declares the route, and
`bz0/d.java` supplies v1.0. Nullable fields are omitted.

These MyQ service requests use Bearer authentication and the recovered native
metadata `BrandId: 1`, `App-Version: 5.324.0.1132442` (`ft0/i0.java`, `y92/b.java`,
`oy0/b.java`). No separate App Check acquisition was established in the reviewed
door action path; App Check is part of the native login investigation.

### Device type names

The native gateway names are `ethernetgateway`, `hub`, `smarthub`, `homebridge`,
`wifigdogateway`, `wifirjo`, `vkpgateway`, `wifichimegateway` and
`wifidashworklightgateway`. Despite its name, `wifilockgateway` resolves to the
lock model and is treated as a lock.

Camera identifiers include `tendcamera`, `bluetooth-camera`, `hawkeye-camera`,
`falcon-camera`, `vkp-camera`, `indoor-camera`, `outdoor-camera`, `doorbell-camera`,
`outdoor-bat-camera`, `doublemint-camera`, `vulcan-camera`, `smartlockcam`,
`smartlockcam1`, `smartptzcam1`, `smartflcam1`, `sgcwlcam1`, `videokeypadcocam`,
`videokeypadcogccam`, `SmartGarageLCCam2`, `SmartIndoorLCCam2`, `petdoor-camera`
and `wifi-camera`. Regional aliases include `smartgaragelcanzcam2`,
`smartindoorlcanzcam2`, `videokeypadcoanzcam`, `outdoor-bat-anz-camera`,
`doorbell-anz-camera` and `smartlockanzcam`. Camera recognition does not guarantee
snapshot, KVS transport or any individual setting. `smartlock` is explicitly a
false-camera placeholder and is not sufficient evidence for lock control.

Bounded searches of the device model/factory and retained Java export found enum
entries for pet doors and generic accessories, but did not establish a complete
pet-door control path. This is an unresolved protocol, not proof that the app or
cloud has no such operation. App remote flags, subscriptions and product rollout
may also affect which enum types actually appear for users.

## Doors, gates and lamps

The door action body is exactly:

```json
{"action":"open","trigger":"myq_an","device_family":"garagedoor"}
```

Closing changes only `action` to `close`. `a01/b.java:H2,c2` constructs the
corresponding `az0/c.java` GDO actions. No stop/percentage member was recovered
in that enum. Gate and commercial-door classes (`model/devices/v.java`,
`d.java`) inherit `GarageDoorDevice`; no separate pedestrian-open, hold-open
or position contract was established in this trace.

`model/devices/u.java:e,l0` normalizes `door_state` case before parsing.
Known integration states are `open`, `closed`, `opening`, `closing`, `stopped`.
Ambiguous `moving`, malformed values and missing state remain unknown.
A reported stopped state does not imply a supported stop command.

The integration permits movement for a recognized door type with family
`garagedoor` when the app's control precheck (`places/view/b.java:C`) would:
the device is online (`k.p1` reads a missing `online` as true), not
monitor-only, has no active PIN lockout (`k.t0`) or work-light error (`k.N0`),
and, for a virtual opener, has no fault on a learned door sensor (`l0.v1`:
`sensor_comm_error` with `number_of_learned_dps_devices > 0`). A missing
unattended-close flag reads as allowed (`k.A1`); the app never reads the open
flag, so a missing one also reads as allowed. An explicit false still denies
that direction. Native monitor-only defaults false for ordinary devices but
**true for `virtualgaragedooropener`** (`model/devices/l0.java:m1`); the
integration preserves that distinction. For openers whose close flag is false,
the app blocks *all* control only for international product-line accounts
(`h0.o`, a country check HA cannot see); in the US and Canada it blocks only
closing, as HA does.
Present malformed permission values grant no action.

Standalone lamps use a different host and no JSON body:

```text
PUT https://account-devices-lamp.myq-cloud.com/api/v6.0/accounts/{account}/lamps/{serial}/on
PUT https://account-devices-lamp.myq-cloud.com/api/v6.0/accounts/{account}/lamps/{serial}/off
```

Sources are `p01/a.java`, `o01/e.java` and `model/devices/d0.java`.
Reported `lamp_state` is on/off; no lamp brightness or color contract was
established. These endpoints are not assumed to control an opener's worklight.

## Residential opener configuration

The configuration service is `https://account-devices-gdo.myq-cloud.com`.
The common route prefix is `/api/v6.0/accounts/{account}/door_openers/{serial}/`.
All three implemented settings use GET for discovery/readback and PUT to change:

| Suffix | Payload | Native/UI meaning |
| --- | --- | --- |
| `lighttimer` | `{"light_timer_setting":150}` | 90, 150, 210 or 270 seconds |
| `timetoclose` | `{"time_to_close_setting":300}` | Seconds on wire; 0 disables, 1–99 whole minutes in native custom UI |
| `worklight_settings` | `{"enable_pir_control":true,"enable_motion_wl":true}` | One combined motion-light switch; native mapper writes both values together |

Sources are `wz0/a.java`, `vz0/z.java`, corresponding `xz0/GDO*V6DTO.java`
types, `yz0/h.java`, `r80/g0.java`, `r80/j.java`, `u50/TimerToClose.java`
and the native light-timer enum in `model/r.java`.

The native worklight mapper requires at least one Boolean field and treats a
missing sibling as false. Its UI also checks remote flag `enable_motion_wl`.
The integration instead discovers support from typed endpoint responses, limits
these settings to residential opener types, and reads support again before a
write. It requires online/non-monitor state; time-to-close additionally requires
unattended-close permission. Complete native owner/co-owner/guest configuration
gates remain unresolved. Service permission errors remain authoritative.

Supported settings are cached for five minutes and unsupported typed/404/405
results for one hour; type/model/capability/firmware changes invalidate the
cache. These durations are HA policies. Optional endpoint failures do not hide
the door or sibling settings. No generic direct opener-worklight on/off command
was established. See [garage evidence](../research/devices-garage-evidence.md).

## Locks

`xy0/h.java` DeviceActuator and `f11/a2.java` LockService select two cloud paths:

| Condition | Exact operation |
| --- | --- |
| `wifilockgateway` or platform `seam` | PUT the common action route with only `{"action":"lock"}` or `{"action":"unlock"}`; no door trigger or family |
| Gateway-connected legacy `lockitron` or other `*_lock` | `POST https://devices-locks.myq-cloud.com/api/v6.0/accounts/{account}/devices/locks/{serial}/lock?requestRemote=true`, or `/unlock`; no body |

Modern source anchors are `f11/a2.java:r2,d2,u2,T0`, `az0/c.java:d,h` and
`zy0/a.java:a`. Legacy anchors are `f11/a2.java:G2,R1`, `d11/a.java:d,e` and
`e11/a.java:j,n`. A `requestRemote=false` request is part of a separate
encrypted Bluetooth workflow, not a complete local lock operation by itself.

Legacy remote access requires a gateway association and
`bluetooth_connectability` of `remote` or `busy`; `local` is a Bluetooth path.
Connectivity follows `LockDevice.p1`: a Wi-Fi lock gateway reports its
camera's `camera_online` whenever that is non-null (`f0.h2`), otherwise the
general `online` flag (missing reads as true). Cloud `lock_state` is parsed
case-insensitively as locked/unlocked/locking/unlocking. Unknown remains unknown.
A lock with neither a gateway nor the action API (Bluetooth only) gets no lock
entity at all; a gateway lock out of remote range is unavailable. Lock commands
are refused while `failed_credential_lockout_active` is true, as the app does.

Commands require a settled locked/unlocked state, non-monitor mode, no
`attached_work_light_error_present`, and unattended-close permission. Unlike the
integration's door rule, the lock path preserves native default true for an
absent unattended-close flag. Present false or malformed flags still deny writes.
An unlock operation does not establish separate open-latch support.

Auto-lock, door-sensor and PIN settings have additional constraints. Legacy
auto-relock uses a Boolean `auto_relock_enabled` and **string**
`auto_relock_time`; native UI offers 30/60/90/120/180 seconds. It checks account
ownership and follows submission with completion verification. Modern
`/api/v1/accounts/{account}/devices/{serial}/lockconfiguration` uses **v1**,
not v1.0. Door-sensor commands use `Enable`/`Disable`, whereas reported state
uses `enabled`/`disabled`. These are not interchangeable generic setting strings.
The integration now implements the legacy owner-gated auto-relock switch/delay
and permitted remote PIN inventory/create/update/delete, with readback/sync
completion checks. Numeric PINs enter through a private local file, not HA
service arguments, traces or diagnostic state. Native `wifilockgateway` also
has an owner-gated automatic-lock delay select (Off, 30/60/90/120/180 seconds)
and an enable switch for an already calibrated door sensor. Its PUT uses the
literal `/api/v1/.../lockconfiguration` route with `Enable`/`Disable`; disabling
automatic lock omits the delay. Reported lowercase state is read back before
completion. This does not add sensor calibration, partner-lock configuration
or modern credential management. Local Bluetooth execution remains separate.
Full chains and current limits
are in [lock evidence](../research/devices-lock-evidence.md) and
[management implementation](management.md).

## Linked thermostats

Every thermostat change is one mode action on the common action route, as the
app's control screen saves it (`t60/i0.java:d0`, called from `e0`):

```json
{"action":"heat_cool","device_family":"hvacs","temperature_unit":"celsius",
 "heating_set_point":20.0,"cooling_set_point":25.0,"fan_mode":"auto"}
```

Mode `heat` sends only the heating point, `cool` only the cooling point, and
`off` neither. `temperature_unit` is the thermostat's own unit; nothing is
converted. The fan travels inside the mode action. A standalone `set_fan_mode`
action exists (`b01/a.java:Q0`, `az0/c.SetFanMode`) but nothing in the app calls
it, so the integration changes the fan by resending the current mode with the new
fan mode. Neither action uses the garage-door trigger. After a 2xx response the
app waits two seconds, re-reads that device and closes the screen; the
integration likewise re-reads two seconds later instead of at once.

Sources are `t60/i0.java:d0,e0`, `b01/a.java:K1,Q0`, `az0/c.java`,
`az0/DeviceActionRequestBody.java`, `zy0/a.java`, `yc1/q.java` and `yc1/i.java`.
`yc1/Specifications.java` defines available modes and min/max heating/cooling
points separately for Celsius and Fahrenheit; `yc1/CurrentClimateSettings.java`
contains current mode/fan/targets. `yc1/DeviceAttributes.java` declares current
temperature, humidity and heating, cooling and fan-running flags.

The app builds each request from its screen's draft. `s60/HvacSetting.java` and
`s60/o.java` (HvacSettingUpdater) define the rules the integration follows:

- **Rounding:** targets are rounded half up to whole degrees Fahrenheit or
  tenths Celsius before sending. Home Assistant converts a target from its own
  unit system into the thermostat's unit first (21 °C becomes 69.8 °F), so the
  value is rounded rather than refused.
- **Gap:** heating and cooling targets stay at least 3 °C / 5 °F apart, exactly
  that gap allowed. Changing one target moves the other to keep the gap; a
  request that sets both too close is refused.
- **Missing targets:** a missing heating point is the cooling point minus the
  gap, a missing cooling point the heating point plus the gap, and with neither
  reported the app starts from 68/78 °F or 20/26 °C.
- **Off:** the app's on/off toggle is always shown; only heat, cool and
  heat_cool follow `available_hvac_mode_settings`. The integration always offers
  off for a controllable thermostat.
- **Draft:** for 30 seconds after a change, the integration builds the next
  request from what it just sent rather than from a readback that may still show
  the previous mode or targets.

Values outside the reported per-mode limits, unknown units and missing limits
prevent the relevant writes.

The native grid mapper passes **not `is_managed`** as subscription-expired
(`com/chamberlain/myq/features/home/mapper/h.java`, `ky/HvacDeviceUiItem.java`).
The integration requires managed state (missing reads as managed, the
`HvacDevice` default) and online/non-monitor state. It no longer checks
`current_climate_setting.manual_override_allowed`, which the app reads only in
preset DTOs. Thermostat enums use Gson's default adapter, so the app reads
names such as `HEAT_COOL` and `CELSIUS` (and writes lowercase `mwString`); the
integration reads them without regard to case and still writes lowercase. A
missing `temperature_unit` means Fahrenheit. Setpoint bounds come from the
`min_*`/`max_*` specifications only; `temperature_threshold` is an alert band,
shown as an attribute. `hvac_action` follows `HvacDevice.j2` (current versus
target temperature); `is_heating`/`is_cooling`/`is_fan_running` are not read.
Partner presets and schedules (`h01/a.java`, v1.0 on
`myq-partner-devices.myq-cloud.com`) are available read-only through the
`get_thermostat_programs` action; the app has no working preset-activation call.

## State confirmation, roles and diagnostics

A successful HTTP acknowledgment establishes neither physical movement nor a
final setting value. The integration reports returned state without optimistic
updates.

Native follow-up polling is defined by `TimeoutConfiguration.kt`
(`model/d0.java`), `DeviceDataSource.kt` (`sy0/a.java`, `sy0/b.java`) and
`DeviceTimeoutHandler.kt` (`uy0/a.java`):

- The app polls only while a device screen subscribes (`DevicePollerImpl.kt`,
  `i31/h.java`; the shared flow stops with its last subscriber). Background
  changes arrive by push.
- Each poll is one GET of the current account's device list
  (`wy0/i.java:K0`); the account itself comes from an in-memory latch. Idle
  polls repeat every 10 seconds.
- A successful command (`xy0/h.java:n`), or a poll reporting `opening`,
  `closing`, `moving`, `locking` or `unlocking`, starts tracking that device
  until it reports its target state (`model/m.java`, companion `c`). The next
  poll runs at once, then every 2 seconds for the garage-door family and every
  second otherwise.
- Tracking ends at the target state or after 60 seconds (openers, lamps and
  other devices), 90 seconds (virtual openers), 120 seconds (gates and
  commercial doors) or 30 seconds (locks). A timeout reports a command error.
  A device still moving afterwards is tracked again on the next poll.

The integration follows the same targets and windows with three differences.
It re-reads every 5 seconds, reads only the tracked devices' accounts during the
window, and does not track a device again while it reports the same moving
state. It has no door/lock push delivery, so it polls at its configured interval
(60 seconds by default) at all times. `moving` is not mapped to a direction, so
it does not start tracking.
Read-only idle Tend monitoring and active camera sessions can invalidate camera
details for an earlier REST refresh; polling remains fallback. This is scoped
to authorized camera aliases and does not implement door/lock FCM delivery.
Per-device locks exclude overlapping in-flight writes, not the entire period
until physical movement ends. A transport-level send guard prevents automatic
mutation replay after a disconnect or server error; this is a local design
choice and is not a guarantee of exactly-once execution by the remote service.
As in the app's authenticator (`ft0/c1`), a 401 with body code `401.102` or
`401`, or no code, is renewed and sent once more, since MyQ did not act on it.
Management operations additionally wait for their own settings, inventory,
schedule or credential readback; pending/sync-in-progress is not success.
Retry-After cooldowns are scoped to service origins, and optional settings,
camera or management polling failures preserve successful device discovery.

Device visibility, advertised capability, account ownership, subscription and
command authorization are distinct. Native settings screens consult additional
account APIs; for example, lock PIN editing excludes Guest role and legacy
auto-relock checks account ownership. The app parses AccountAccess permission
lists but never reads them; its screens are gated on the role, and ownership
comes from the account's `created_by`. The integration does the same: see
[management authorization](management.md#authorization) for the role each
operation needs. New management writes re-read the role. Cloud rejection remains
authoritative.

The integration exposes allowlisted returned state: connectivity, door/lamp/lock
state, battery/backup battery, cycles, Wi-Fi signal, sensor faults, monitor and
remote-operation flags, lock mode ("Remotes disabled", `in_vacation_mode`), lock
connection and reported auto-lock configuration, plus the additions listed in
[app parity review](../research/app-parity-review.md). Lock door-contact state is meaningful only when its sensor is
enabled. Every discovered record gets a device-type diagnostic. Downloadable
diagnostics omit serials/account IDs, names, tokens, credentials and arbitrary
raw values; they include types, family, known status and field names. Presence of
other DTO fields does not automatically make them safe or useful diagnostics.

## Management surfaces and unresolved device types

The implementation now exposes typed opener accessory inventory, supported
transmitter rename/assignment/removal, legacy remote lock PIN/auto-relock
management, existing guest inventory/access/removal, and camera workflow
schedules. Initial configuration creation is limited to recovered feature/model
gates: PTZ privacy/patrol, advertised floodlights and voice deterrence. It rejects
an existing configuration rather than duplicating it. It validates account/device/feature
associations and verifies completion. Guest access edits preserve existing
date/time/recurrence/timezone restrictions. See
[management implementation](management.md) for exact wire fields,
role gates, nonsecret responses and remaining gaps.

Pairing/learn mode, opener lock-mode writes, gateway/Wi-Fi setup, firmware, new
invitations, broader credentials/workflow provisioning, alerts and
account/device removal still need complete execution and policy traces. An
endpoint declaration alone is not an implementation. The
[garage ledger](../research/devices-garage-evidence.md) and [lock ledger](../research/devices-lock-evidence.md)
record the source inventory.

Accessory types include remotes, S3 keypads, HomeLink and wall controls. The
`petdoor`, `remoteled` and `garagedooropeneraccessory` enum entries did not yield
a complete actuation path in the bounded reviewed sources. `smartlock` is a
false-camera placeholder, not proof of lock control. Gateways and unknown types
therefore receive identity/known diagnostic state without guessed commands.
This does not prove that the app/cloud lacks those operations.

Native runtime behavior, real inventory and roles, device-specific failures and
current server compatibility remain unverified. The
[coverage record](../research/coverage.md) lists the remaining work.

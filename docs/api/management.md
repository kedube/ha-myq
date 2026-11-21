# Management

Account roles, and the management operations built on them: opener
accessories, lock configuration and PINs, guest access and camera schedules,
from MyQ Android 5.324.0.1132442. The integration's actions are listed in the
[actions guide](../integration-actions.md). These routes have not been exercised
against a live account or device.

## Authorization

`an0/a` declares `GET accounts.myq-cloud.com/api/{apiVersion}/accountAccess`;
`zm0/b.c` supplies `v6.0`. `bn0/AccountAccessDto`, `AccountAccessEntry` and
`Permissions`, together with `cn0/a` and `cn0/d`, establish this shape:

```json
{
  "account_access": [{
    "account_id": "account-id",
    "role": "owner",
    "permissions": {
      "devices": ["View", "Control", "Manage", "Delete"],
      "guests": ["All"],
      "schedules": ["All"]
    }
  }]
}
```

`rm0/AccountRole` aliases are `owner`, `co_owner`, `guest`, `family`,
`guest_nomyquser` and `quick_pass`; the role mapper (`cn0/d`) lowercases the value
first. The AccountAccess mapper excludes QuickPass. Permission aliases are `All`,
`Manage`, `View`, `Control`, `Delete`, matched without regard to case.

**The app never reads the permission lists.** `cn0/a` parses them into
`rm0/AccountPermissions`, but that class has no reader: only the mapper and
`rm0/AccountRole` (which stores it without a getter) refer to it. Every screen is
gated on the role (`isOwner`, `isCoOwner`, `isGuest`) instead. Owner is not a role
check at all: `accountApi.o()` resolves through `nc2/b.o` and `ke2/a.o` to
`myq/sdk/common/misc/h0.k`, which compares the active account with the account
whose `created_by` is the signed-in profile (`xm0/b.m`). So the app knows the
owner even when AccountAccess cannot be read.

The integration follows the app: it parses the lists but gates nothing on them.
An account counts as owned when its `created_by` (from the accounts list) is the
signed-in user, or when its role is `owner`; that ownership holds even if
AccountAccess fails. Each kind of operation is offered to the roles whose app
screens offer it:

| Operations | Roles | App evidence |
| --- | --- | --- |
| Live view, camera controls, recordings | Every known role | No role check in the live, PTZ, siren, talk or clip screens |
| Alert rules (read and switch) | Every known role | Notifications row `y80/g.o` |
| Alert snooze | Owner, co-owner | `main/i5` (`accountApi.G()`) |
| Device history, guest list, legacy lock PINs | Every role but guest | History and Users tabs (`dm0/d`, `dm0/e`); PINs `b70/x0.r0` |
| Native Wi-Fi lock automatic lock and door sensor, PIN lockout reset | Owner, co-owner | `y80/p`, `y80/h`; lockout clear flow `sx/z`, `c70/a` |
| Opener settings, lock mode, rename, accessories, legacy auto-relock, camera settings and schedules, guest changes | Owner | `y80/o`, `y80/k`, `y80/x`, `y80/i`, `y80/g.j`, `y80/t` |

Unknown roles and malformed entries authorize nothing. A fresh AccountAccess read
precedes each management write. `is_managed` is not an ownership flag.

## Implemented routes and completion

| Capability | Native evidence | Implemented behavior |
| --- | --- | --- |
| Legacy auto relock | `e11/a.h`, `d11/a`, `c11/TimerToLock`, `j70/a`, `k31/b` | Owner-only switch and delay select: 30, 60, 90, 120, 180 seconds. PUT lock `/auto_relock?requestRemote=true`, boolean `auto_relock_enabled`, **string** `auto_relock_time`. Requires explicit capability and a remote gateway. Poll reported enabled/time fields before success. |
| Native Wi-Fi lock automatic locking | `r80/h.f`, `u50/d`, `g11/a`, `cz0/a.C`, `bz0/d.h`, `bz0/e.b`, `cv0/o`, `AutoLockOption` | Owner or co-owner delay select with Off and 30/60/90/120/180 seconds. PUT devices-action literal `/api/v1/accounts/{account}/devices/{gateway}/lockconfiguration`. Positive delays send `automatic_lock_state:"Enable"` and integer `automatic_lock_delay_duration`. Off sends `automatic_lock_state:"Disable"` and omits the delay. Confirm reported state and positive delay. Only native `wifilockgateway` devices with observed fields are eligible; this is not a Seam partner-lock API. |
| Native Wi-Fi lock door sensor | `sb0/f2`, `az0/AutoLock`, `g11/a`, `bz0/e.b`, native Device `p0/n0` | Enable/disable an already calibrated sensor through the same literal v1 route using only `door_sensor_state:"Enable"`/`"Disable"`. Owner or co-owner required; a sensor switch appears only with observed sensor state and existing calibration. Readback confirms enabled/disabled. Calibration/provisioning is a separate workflow. |
| Accessory inventory | `wz0/a.q`, `vz0/z.U/V`, `xz0/TransmitterListDto`, `TransmitterDto` | Owner only (Remotes & Keypads, `y80/o`). GET opener `/accessories` and parse paired transmitter identity/type/name/deletion/battery/assignee/gateway the way `yz0/g.a` does: a null list is empty, a missing deletion flag false and a missing name empty; a contradicting `gdo_serial_number` is refused and an entry without a unique serial is skipped. Optional battery enum entities are disabled by default. No fabricated battery percentages. |
| Accessory rename | `wz0/a.h`, `vz0/z` call to `h` | PUT opener `/{type}/rename` with `name`, `tx_serial_number`, `is_delete_in_progress:false`. As `s50/e1.m0`/`c0`, the name has 1 to 24 characters and may not match another accessory on the opener, ignoring case. Readback requires the same serial and new name. |
| Accessory removal | `wz0/a.t`, `vz0/z` call to `t` | PUT opener `/{type}/{serial}/send_remove`, no body. `is_delete_in_progress:true` is pending, not success; removal must be observed in fresh inventory. |
| Accessory assignment | `wz0/a.s`, `vz0/z` call to `s` | PUT opener `/{type}/assign`, same rename fields plus `account_user_id`. Assignee must exist in the account and have explicit access to the opener. Owner required. Read back assignee. |
| Camera workflow schedules | `ly0/j`, `d21/g`, `c21/f`, `l60/k2`, `l60/s`, `l60/j1`, `kw0/e` | Read, create, update, enable/disable and delete schedules for `floodlight`, `patrol_mode`, `privacy_mode`, `voice_deter`. Existing configurations and schedules must belong to the queried device and feature. First configuration creation uses fresh native capability/model gates. Empty existing configurations are accepted. Owner required. Reads follow `c21/f.d`: missing text is empty, a missing flag disabled, and `count` is not read. As `l60/t.f0`/`k0` (`l60/l1.b`), an enabled schedule may not overlap another enabled schedule of the configuration; a window whose end is not after its start runs past midnight. Readback verifies identity and changed fields or absence. |
| Legacy lock PIN inventory | `e11/a.f`, `c11/PinList`, `c11/Pin` | Return only ID, name, access and normalized status. Numeric PIN values are never included in the model or response. |
| Legacy lock PIN create/update/delete | `e11/a.e/q/l`, `d11/a`, `w21/a0.w/x/u`, `w21/y.I/Y/b0/J`, `l31/b`, `h70/k1.g0/j0` | Remote create/replace/delete with `requestRemote=true`, explicit PIN capability and any role but guest (`b70/x0.r0`). Create uses `pin`; replace uses `updated_pin`; both use `name` and observed native default `access:"enabled"`. Wait for `synced` after a `pending` status, which the PUT answer itself may carry (`l31/b`); `duplicate`/`failed` are errors. Delete requires absence and stops on a `failed` status (`l31/d`). |
| Guest inventory | `in0/b.c`, `hn0/j.o/n`, `gn0/b.a`, `jn0/GuestsAccountsDto` | GET guestaccess `/api/v6.0/accounts/{account}/users`, parse `account_users`, roles and `access_schedules[].allowed_devices`. Email, birthday and user identity fields unrelated to management are omitted. |
| Guest device access | `in0/a.b`, `hn0/b.c`, `tn0/a.c`, `sn0/AccessScheduleDto` | Owner may update the allowed-device list on an existing guest access schedule. Every serial is checked against fresh account inventory, and a newly added device must be one the app's guest pickers offer (`iv0/c.b`: an opener, gate, lamp, video keypad, Lockitron or Wi-Fi lock gateway type, or a device reporting a camera, with a serial containing `CG06`, `CG08`, `CG0A`, `LTRON`, `GW`, `CG28` or `CG091`). The app first deletes the guest's PINs on a video keypad being removed (`vk0/h.X`, unless `enable_biometrics_management` is on); the integration cannot read that PIN service, so it does not, and its action description says so. Existing date, time, recurrence and timezone restrictions are preserved. PUT `/users/{user}/accessSchedules/{schedule}` omits the schedule ID from the body, matching native mapper. Confirm the new device list and unchanged restrictions. |
| Guest removal | `in0/b.a`, `hn0/j.m`, `gn0/b.c` | Owner may DELETE an existing guest/guest-without-MyQ account; owner/co-owner/family/unknown roles are excluded. Confirm absence. Before revoking, the app deletes the guest's keypad PINs (`f40/b0.g` → `f40/y4` → DELETE `devices/{serial}/pins/{id}` on the user-device-PIN service, `td1/a`). That service's host is configured at run time and was not recovered, so the integration does not delete them; its action description tells users to remove them in the app first. |

Accessory type routing follows `fe0/m7`, `s50/e1`, native `TransmitterType` and
`Transmitter`: `OokTx`, `S3Transmitters`, `S3Keypads`; HomeLink uses `FhssTxs`
for serial prefixes `TX02`/`TX0C`, otherwise `OokTx`. Unknown transmitter types
remain inventory-only. Known wall-control discovery does not imply that it can
be renamed, reassigned or removed by these services.

Workflow host is `https://workflows.myq-cloud.com`; version is `v1.0`, not the
v6.0 scene API. List query parameters are `serialNumber` and `feature`.
Schedule DTO fields are `label`, `enabled`, `days`, `start_time`, `end_time`.
Native weekday encoding is lowercase names separated by `, ` in Monday–Sunday
order, and time encoding is `HH:mm:ss` at minute resolution. No timezone field
is sent to this DTO; schedule times follow the native account/device service
interpretation. This is distinct from guest access schedules, which explicitly
carry timezone and their own recurring-day encoding.

First configuration creation follows `l60/k2.b` and `c21/f.a`: POST the scenes
collection with `enabled`, `action.devices:[{enabled:true, serial_number,
feature}]` and a single typed schedule. The response must contain a valid
`id` or `configuration_id`, and the subsequently queried device/feature
inventory must confirm that same configuration and schedule. A configuration
that already exists must be edited through its ID rather than duplicated.
Native entry gates are recovered from `c60/s3`, `k80/h`, `n60/l0`, `r70/k0`,
`l00/b` and `xz/q`: privacy/sleep scheduling requires the PTZ model
`smartptzcam1` plus `cap.cam.enableSwitch`; patrol requires that model and at
least two distinct saved presets, read from `settings.cam.ptz.presets` and
bounded by `cap.cam.ptz.maxPresets`; floodlight requires a reported floodlight
entry in `cap.lightList`; voice deterrence requires `cap.cam.voiceDeter:true`.
The integration rejects malformed capabilities and drifted preset state.

The modern automatic-lock UI resolves a smart-lock camera to its native gateway
using `yc1/f.s` (`gateway_id`/`gatewayID`, then `parent_serial_number`). The
integration exposes the setting on the already-discovered `wifilockgateway`
device and never guesses that association from a camera serial. Native
`r80/h.A0/z0` confirms `automatic_lock_state == "enabled"` plus the requested
delay, or disabled state for zero. Reported values use lowercase; requests use
the imperative `Enable`/`Disable` strings.

Remote PIN execution does not require implementing the local BLE encrypted
relay: `w21/y.b0` selects the cloud PIN status poller when `requestRemote` is
true. Native poll configuration is five-second intervals with a 60-second
limit. The implementation performs at most 12 readback attempts and never
replays a mutation. Names have a local 64-character input bound; PIN input has
a local 4–16 ASCII-digit bound. The native validator `i70/b` requires at least
four characters; additional model-specific limits are enforced by the service.

Replacing a PIN requires readback to observe a fresh `pending` to `synced`
transition with the requested name and enabled access. The old PIN has the same
ID and may have the same name, so unchanged `synced` metadata cannot confirm a
new numeric credential. If the transition is missed, the operation reports an
uncertain outcome without resending the mutation. Newly created PINs can confirm
immediate synchronization because their returned identity must be new.

## Integration hooks

`Management(client)` exposes these typed methods:

| Method | Arguments beyond `device` |
| --- | --- |
| `account_access` | No device argument; returns `dict[account_id, AccountAccess]` |
| `require_access` | `account_id, resource, permission, owner=False` |
| `set_auto_relock` | `enabled: bool, seconds: int` |
| `set_auto_lock` | `seconds: int`; zero disables, other native options enable |
| `set_door_sensor` | `enabled: bool`; configured, calibrated native gateway only |
| `accessories` | None |
| `rename_accessory` | `accessory_serial, name` |
| `remove_accessory` | `accessory_serial` |
| `assign_accessory` | `accessory_serial, account_user_id` |
| `schedules` | `feature` |
| `configuration_ids` | `feature`; synchronous IDs from the latest successful `schedules` read, including configurations with no schedules |
| `set_schedule_enabled` | `feature, configuration_id, schedule_id, enabled` |
| `save_schedule` | `feature, configuration_id`, keyword `label, days, start_time, end_time, enabled, schedule_id=None` |
| `create_schedule` | `feature`, keyword `label, days, start_time, end_time, enabled`; first native configuration only |
| `delete_schedule` | `feature, configuration_id, schedule_id` |
| `pins` | None |
| `save_pin` | Keyword `name, pin, pin_id=None` |
| `delete_pin` | `pin_id` |
| `guests` | None |
| `set_guest_devices` | `account_user_id, access_schedule_id, allowed_serials` |
| `remove_guest` | `account_user_id` |

`management_switch_entities`, `management_select_entities` and
`management_sensor_entities` consume coordinator `account_access` and
`accessories` snapshots. Existing auto-relock entities become unavailable when
ownership, grants, capability or connectivity changes. Native gateway automatic
lock delay and calibrated door-sensor entities use the same dynamic checks.
There is no generic
endpoint, request body or arbitrary action service.

The service layer supplies PIN data through a private file instead of raw HA
service arguments so that numeric PINs do not enter HA call-service events or
automation traces. The API does not retain the submitted credential, returns
no numeric PIN, and uses fixed errors without server bodies. Metadata and
personal identity fields are excluded from dataclass representations.

## Completion and remaining boundaries

Management operations for the same device are rejected while another is in
progress; guest access changes are additionally serialized at account scope.
Each network mutation uses the shared client device lock and sends once.
Management readback continues under the management lock. Unrelated physical
door commands can run after submission; they cannot satisfy management
confirmation because verification examines the separate settings, PIN,
accessory, guest or workflow fields. A conflicting external change produces
an uncertain outcome, without replay or rollback.

The following native declarations still lack enough recovered execution or
policy semantics for additional writes:

- New guest invitations and role upgrades: birth-date, minor/family roles,
  verification and invitation-policy semantics differ by API version. Existing
  guest access and removal are implemented; there is no generic invitation
  payload service.
- Seam partner-lock automatic configuration: the recovered native gateway
  setter does not establish partner support. Such settings are not sent to
  the native gateway route solely because a device has a lock suffix.
- Accessory learning, calibration, firmware and provisioning retain native
  multi-stage execution requirements and are not substituted with raw HTTP
  writes.

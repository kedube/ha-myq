# Lock device evidence

This ledger records static Android code findings, not observed cloud execution.
Target: MyQ Android **5.324.0.1132442**, APK SHA-256
`20d6e5d35a819169867f1b7ebbe13769720ae0b78cb6e26ef95bc9f36dabefb2`.
Sources are the existing local JADX 1.5.7 export, kept outside the repository.
The overall decompilation has errors; the specific method bodies below were
readable. The parent investigation records artifact provenance. No credential
or device operation was performed during this investigation.

In the paths below, `model/` abbreviates
`com/chamberlain/myq/sdk/devices/services/external/feature/common/model/`.

## Device families and state

| Observation | Source |
| --- | --- |
| `lockitron`, vendor types ending in `_lock`, and `wifilockgateway` resolve to the lock device model. The factory also supports numeric type 18. | `model/o.java` DeviceType; `model/f.java` DeviceFactory `a`, `b` |
| `smartlockcam`, `smartlockcam1`, and `smartlockanzcam` resolve to LockCamera, which extends Camera. A Wi-Fi lock gateway is a separate lock record. | `model/f.java`; `model/devices/e0.java` |
| `smartlock` is the distinct `SMART_LOCK_FALSE_CAMERA` enum. It is not sufficient evidence for using either lock command route. | `model/o.java`, `model/f.java` |
| Cloud state comes from `state.lock_state`; known values are case-insensitive `locked`, `unlocked`, `locking`, `unlocking`. Local-only locks instead consult the Android Bluetooth service. | `model/devices/f0.java` `e`, `l0`; `model/m.java` DeviceState `Companion.b`; `yc1/DeviceAttributes.java` |
| `device_platform` and `gateway_id` are top-level fields. The gateway getter falls back to `state.gatewayID`. | `yc1/f.java` declarations and `r` |
| Seam and `wifilockgateway` use the remote path. Other locks require a gateway association, connectivity, and `bluetooth_connectability` equal to `remote` or `busy`. `local` is a Bluetooth path. | `model/devices/c.java` `R1`, `T1`; `iv0/a.java` `R`; `iv0/c.java` `F`; `model/devices/k.java` `r1` |
| Lock UI control is restricted by monitor-only, unattended close permission, attached-work-light fault, and a usable state. Native unattended-close permission defaults to true here. | `model/devices/k.java` `X0`, `A1`, `m1`, `N0`, `v1`; `model/devices/f0.java` `y1` |
| Wi-Fi lock connectivity also consults `camera_online`. | `model/devices/f0.java` `p1`, `h2` |

The integration discovers the proven string types. It does not infer lock
commands from a numeric type alone, a camera name, or a model number. Bluetooth-only
lock entities are unavailable because their state and commands require the
Android Bluetooth service. Unknown cloud lock state remains unknown. The four
known lock states are displayed without optimistic updates. An unlock command
does not advertise Home Assistant's separate open-latch capability.

## Implemented remote operations

`xy0/h.java` DeviceActuator `m` (lock), `y` (unlock), and `x` (desired state)
select the two paths. `f11/a2.java` LockService forwards them as follows:

| Type/platform | Operation | Wire contract |
| --- | --- | --- |
| `wifilockgateway` or platform `seam` | Lock / unlock | `PUT https://devices-action.myq-cloud.com/api/v1.0/accounts/{account}/devices/{serial}` with `{"action":"lock"}` or `{"action":"unlock"}` |
| Legacy `lockitron` or `*_lock`, with confirmed gateway remote access | Lock / unlock | `POST https://devices-locks.myq-cloud.com/api/v6.0/accounts/{account}/devices/locks/{serial}/{lock|unlock}?requestRemote=true`, no body |

Modern methods are `f11/a2.r2`, `d2`, `u2`, `T0`; the action enum implementations
are `az0/c.d` and `az0/c.h`. `zy0/a.a` constructs a DeviceActionRequestBody with
only the action populated for locks. The nullable trigger and device-family
fields are omitted. The Retrofit contract is `bz0/e.a`; `bz0/d` supplies v1.0.

Legacy methods are `f11/a2.G2` / `R1`, `d11/a.d` / `e`, and `e11/a.j` / `n`.
`d11/a` uses v6.0. Host identity comes from `oy0/b.LOCKS` (`devices-locks`).
The usual native `BrandId`, `App-Version`, and OAuth bearer headers apply.
The legacy `requestRemote=false` branch also invokes a Bluetooth operation;
sending that HTTP request alone would not implement the operation.

The integration's `api/locks.py` requires online state, a proven remote path,
an idle known lock state, and nonrestricted device flags (including no active
PIN lockout) before writing. Connectivity follows `LockDevice.p1`: a Wi-Fi lock
gateway uses `camera_online` whenever it is non-null, otherwise `online`
(missing reads as true). It rejects overlapping writes
using the client-wide per-device mutation lock. Uncertain network outcomes
never replay a mutation; a renewable 401 is renewed and sent once more, as the
app does. Home Assistant refreshes
discovery after the operation, and only observed state changes update the lock.

## Settings and management

These features are present in the app but do not justify a generic lock-settings
mutation endpoint or unrestricted management controls.

| Feature | Established behavior | Integration boundary |
| --- | --- | --- |
| Legacy auto relock | `e11/a.h`: `PUT /api/v6.0/accounts/{account}/devices/locks/{serial}/auto_relock?requestRemote=...`. `c11/TimerToLock` body is boolean `auto_relock_enabled` plus **string** `auto_relock_time`. `j70/a` exposes 30, 60, 90, 120, 180 seconds. | Read-only reported state. No settings write until account ownership and execution completion are represented. |
| Legacy timer permission | `b70/x0.s0` requires `allow_auto_relock` and `accountApi.o()`. That delegates through `nc2/b.o`, `ke2/a.o`, to `myq/sdk/common/misc/h0.k`, comparing active account ID with `accountService.h()` ignoring case. | Do not substitute `is_managed`, a guessed role string, or mere device visibility for this account check. |
| Timer execution | `j70/q.U` -> `wx0/c.l` -> `w21/a0.l` -> `w21/y.i0`, `a0` -> `f11/a2.C1`. Successful remote submission starts `k31/b` to verify settings; local execution uses the encrypted Bluetooth pipe. | HTTP acknowledgement alone is not evidence that auto relock was configured. |
| Wi-Fi lock configuration | `bz0/e.b`: `PUT /api/v1/accounts/{account}/devices/{serial}/lockconfiguration` (v1, not v1.0). `az0/AutoLock` has nullable `automatic_lock_state`, `door_sensor_state`, `automatic_lock_delay_duration`. | No generic configuration writes. Full settings setter/range and permission traces remain incomplete. |
| Door sensor provisioning | `sb0/f2` creates AutoLock with only `door_sensor_state="Enable"` or `"Disable"`, via `g11/a.l2` -> `zy0/a.C` -> `bz0/d.h`. These command strings differ from reported `enabled` / `disabled`. | Read existing sensor state; provisioning and calibration stay in the app. |
| Door sensor state | `f0.r0` / `devices/n` decode `enabled` / `disabled`; `f0.s0` / `devices/o` decode `open` / `closed`; `devices/m` distinguishes calibrated/not calibrated. | Report only known state, with door-contact visibility conditioned on sensor enabled. |
| PIN management | `e11/a` declares PIN list/read/create/update/delete/sync routes. `b70/x0.r0` gates by `allow_pin_code` and not `accountApi.f()`. `ke2/a.f` -> `h0.j` tests the Guest role. `w21/y` routes remote and encrypted local operations differently. | No PIN credential entities, service inputs, writes, or secret diagnostics. Complete access semantics and a dedicated secure UI remain unimplemented. |
| Installation, firmware, orientation, rename, secure delivery, schedules, and removal | Visible settings actions and fields exist in `b70/x0`, `com/chamberlain/myq/features/places/devices/hf`, `e11/a`, and encrypted-pipe interfaces. | No untraced provisioning, firmware, delivery, removal, or schedule writes. |

`is_managed` is consumed by the Seam integration/subscription UI
(`com/chamberlain/myq/features/places/devices/yf.f`); it is not a proven universal
lock-actuation permission. Similarly, `model/devices/k.A0` inspects fault codes,
not an account role. No additional account role predicate was found in the
traced lock/unlock dispatch path. Cloud authorization continues to apply.

Useful nonsecret reported fields include `low_battery`
(`yc1/DeviceAttributes.java:294`),
`battery_percentage` (a string in the DTO), `auto_relock_enabled`,
`auto_relock_time` (integer in the state DTO), `allow_auto_relock`,
`door_sensor_state`, and `door_sensor_calibration_status`. Bluetooth
`access_key` and `broadcast_id` are not exposed as diagnostics.

## Validation and remaining uncertainty

`tests/test_locks.py` validates exact method/host/path/body/header contracts for
both remote routes, state normalization, malformed/missing permissions,
Bluetooth-only rejection, distinct camera records, shared mutation serialization,
nonreplay on a non-renewable 401 or a 503, and real Home Assistant lock discovery/state/services with
synthetic responses. No real lock has been operated, and no runtime account
inventory or server acceptance of these routes has been verified.

Account ownership context, configuration completion polling, modern automatic
lock setter values/ranges, guest/credential lifecycle semantics, and Bluetooth
provisioning remain explicit limitations. The implementation does not claim
complete parity with the app's lock-management screens.

## Recovering the automatic-lock setter

The modern auto-lock setter was absent from the original export because JADX
failed to restructure `r80/h.f.invokeSuspend`. A single-class export with
`--show-bad-code --comments-level debug` recovered the payload, and an independent
`--decompilation-mode simple` export completed without errors and confirmed the
instruction sequence. The two focused exports, `auto-lock-debug.java` and
`auto-lock-instructions.java`, are retained locally, outside the repository.

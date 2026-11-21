# Garage doors, gates, lamps and opener accessories

This ledger records static evidence from MyQ Android **5.324.0.1132442** and the
resulting integration behavior. It does not establish that the current cloud
accepts these requests for a particular account or product. No live cloud writes,
door movements or provisioning operations were performed.

The investigation reused the extracted APK and its JADX output, a local export
kept outside the repository. Source paths below are relative to the export's
sources directory. The decompiled Java includes some failed coroutine
reconstructions, which limits conclusions about complete UI workflows. This
document describes protocol facts and behavior without copying application
implementation code.

## Device families and operational limits

| Native device type | Evidence | Integration behavior |
| --- | --- | --- |
| `garagedooropener`, `wifigaragedooropener` | `com/chamberlain/myq/sdk/devices/services/external/feature/common/model/o.java` | Garage cover state and permitted open/close; optional settings below. |
| `virtualgaragedooropener` | Same enum; `.../devices/l0.java` | Learned door/sensor cover. Native monitor-only default is **true**, unlike the base device default. No assumption that opener settings exist. |
| `commercialdooropener` | Same enum; `.../devices/d.java` | Native class inherits `GarageDoorDevice`; existing guarded cover operations apply when the device reports the supported family and permissions. No distinct commercial configuration protocol established here. |
| `gate` | Same enum; `.../devices/v.java` | Native class inherits `GarageDoorDevice`; existing guarded cover operations apply when permitted. No separate gate stop, position, pedestrian-open or hold-open operation established here. |
| `lamp` | Same enum; `.../devices/d0.java` | Reported `lamp_state`, on/off control. No brightness, color or temperature command established. |

`.../devices/u.java` normalizes `door_state` before interpreting it. The native
door action enum in `az0/c.java` has `open` and `close`; `a01/b.java` routes these
through the device action repository. These findings do not justify adding
stop, percent-position or arbitrary action services. The existing door command
uses the action service, rather than the older opener-specific open/close
methods also declared in `wz0/a.java`.

The integration requires an online device, supported `garagedoor` family,
non-monitor operation and the explicit relevant `is_unattended_*_allowed` flag
for movement. Native model default values are not sufficient evidence to grant
an unknown account permission. `.../devices/l0.java:m1` specifically defaults
`monitor_only_mode` to true for a virtual opener. Its connectivity helper also
uses `sensor_comm_error` and `number_of_learned_dps_devices`; those fields are
status evidence, not commands.

## Implemented residential opener settings

The service host is `https://account-devices-gdo.myq-cloud.com`:
`oy0/b.java` maps `GDO` to `account-devices-gdo`, `zz0/a.java:i` constructs the
service, and `vz0/z.java` selects API version `v6.0`.

All three settings use this path prefix:

```text
/api/v6.0/accounts/{accountId}/door_openers/{serialNumber}/
```

| Suffix | Methods | Read/write JSON | Home Assistant representation |
| --- | --- | --- | --- |
| `lighttimer` | GET, PUT | `{"light_timer_setting":150}` | Select: 90, 150, 210 or 270 seconds. |
| `timetoclose` | GET, PUT | `{"time_to_close_setting":300}` | Number: 0–99 whole minutes, converted to seconds; zero disables. |
| `worklight_settings` | GET, PUT | `{"enable_pir_control":true,"enable_motion_wl":true}` | One motion-activated opener-light switch; both values written together. |

Exact source ledger:

- `wz0/a.java:i,n` declares light timer PUT/GET; `xz0/GDOLightTimerV6DTO.java`
  provides its JSON field. `.../feature/common/model/r.java` defines the four
  durations. The constant used for its 210-second option resolves in
  `com/onetrust/otpublishers/headless/Public/OTUIDisplayReason/OTUIDisplayReasonCode.java`.
- `wz0/a.java:r,u` declares time-to-close PUT/GET;
  `xz0/GDOTimeToCloseV6DTO.java` provides its JSON field.
  `u50/t.java` passes reads and writes to the GDO service. `u50/TimerToClose.java`
  supplies the default maximum of 99 minutes. `r80/j.java:z0` validates the custom
  duration; `com/chamberlain/myq/features/places/devices/g8.java` includes off,
  1-, 5- and 10-minute presets plus custom duration. `.../devices/th.java` converts
  UI minutes to seconds for writes and converts seconds back for display.
- `wz0/a.java:c,x` declares worklight settings PUT/GET;
  `xz0/GDOWorklightSettingsV6DTO.java` defines two nullable Boolean fields.
  `yz0/h.java` rejects a response with neither field, but treats a missing sibling
  as false when one field exists. `r80/g0.java:X,c0` displays their logical OR and
  writes both to the requested switch value. `r80/g0.java:a0` also checks the
  remote application feature flag; `ra2/c.java:x2` names it `enable_motion_wl`.

The API implementation in `api/garage.py` restricts discovery to residential
openers and requires a correctly typed successful response for each setting.
This is observed support discovery; the integration does not invent capability
names or assume every opener supports these settings. It does not import the
Android app's remote feature-flag configuration. It uses device support responses
and server authorization instead.

Writes require current online/non-monitor status and a fresh successful setting
read. Time-to-close additionally requires explicit unattended-close permission,
because enabling it can cause a future automatic closure. Owner/co-owner/guest
role rules for every native configuration screen were not completely traced;
server permission rejection remains authoritative. Unknown or malformed values
produce no control entity.

Supported reads are cached for five minutes; unsupported typed responses and
404/405 results are cached for one hour. Model, type, capability or firmware
changes invalidate cached discovery. Transient failures do not permanently
disable a setting, and one optional endpoint failure does not disable its
siblings or the garage cover. Authentication and rate-limit errors propagate.
Every write uses the shared device mutation lock and is sent once. The cache is
invalidated after success or uncertainty so a later reported value confirms the
state; HTTP acknowledgment is not treated as device confirmation.
Per-setting revisions also prevent a delayed read started before or during a
mutation from restoring the invalidated cache, including unsupported responses.

There is no verified generic **opener worklight on/off** command in this trace.
Motion activation and light duration are configuration settings. The native
`WorklightLightSettingsScreenTracker` in
`com/chamberlain/myq/features/places/devices/pi.java` identifies `dash_worklight`;
its on/off and brightness controls belong to a camera/worklight product and must
not be reused for an ordinary opener or lamp.

## Accessories and management evidence

`wz0/a.java:q` declares a read at the same opener prefix plus `accessories`.
`vz0/z.java:U,V` decodes `xz0/TransmitterListDto.java`:

```text
gdo_serial_number
gdo_transmitter_names[]:
  name, tx_serial_number, is_delete_in_progress, account_user_id,
  transmitter_type, battery_state, tx_variant, gateway_serial_number
```

`xz0/TransmitterDto.java` defines these fields. The native domain model
`.../devices/Transmitter.java` distinguishes `OokTx` remotes, `S3Transmitter`
remotes, `S3Keypad`, `FhssTx` HomeLink and `S3Wwc` wall controls. Battery values are
`good`, `low`, `critical` and `unknown`. Wall-control variants include wall-powered,
battery-powered and doorbell models. These accessories are paired inputs or
controllers; their presence does not establish independently addressable door,
light or keypad actions.

Additional native management declarations in `wz0/a.java` include:

| Operation | Declared method and route suffix |
| --- | --- |
| Read pairing result | GET `{serial}/accessories/{accessorySerial}/paired` |
| Rename transmitter | PUT `{serial}/{transmitterType}/rename` with `TransmitterDto` |
| Assign transmitter | PUT `{serial}/{transmitterType}/assign` with `TransmitterDto` |
| Remove transmitter | PUT `{serial}/{transmitterType}/{txSerial}/send_remove` |
| Start learning | PUT `{serial}/start_learning` |
| Enter/exit all learn mode | PUT `{serial}/enter_all_learn_mode`, `exit_all_learn_mode` |
| Opener lock mode | PUT `{serial}/lockmode` with `{"enable_lock_mode":true}` |
| Pair/unpair/reorder door openers | POST `door_openers/pair`, `unpair`, `reorder` under the account path |

For the table, `{serial}` is relative to
`/api/v6.0/accounts/{accountId}/door_openers/`. The start-learning DTO in
`xz0/StartLearningRequestDto.java` contains `fixcode`, `device_learn_mode`,
`timeout` and `button_id`; its mode enum includes `Fhss`, `All`, `AutoLearn` and
`None`. These declarations alone do not establish complete provisioning,
confirmation, role, timeout or recovery behavior.

`GET .../door_openers/transmitter?mainControlSerialNumber=...` is a separate
door-controller lookup, not the accessories response described above.

Learning remains deferred. Opener lock mode is now implemented as the
**Disable remotes** switch: PUT `lockmode` with `{"enable_lock_mode": bool}`
(`wz0/a.java:26`, `xz0/LockModeRequestDto`), read back from
`state.in_vacation_mode` (null reads as false, `iv0/b.g` -> `p067r50/c2.java:224`).
The app offers the row on the Remotes & Keypads screen only to the account
owner, on a Wi-Fi opener or an opener whose gateway (or own) serial starts with
`3C` after any `GW` prefix (`y80/o.java:79-84`, `iv0/c.k`). The integration
adds the devices Manage grant, online and not monitor-only. Opener settings and
accessory writes are likewise owner-only, as `GdoDeviceSettingsProvider` shows
them, and non-owners cost no settings reads. The integration does not expose
generic calls for incomplete workflows.

## Local validation

`tests/test_garage_settings.py` covers native paths, headers, bodies and units;
nullable and malformed fields; support caching and invalidation; isolated read
failures; permission changes; fresh support checks; and no command replay after
401 or uncertain responses. The focused suite passed **37 tests**. Ruff passed
for the two implementation files and this test file. Hardware and authenticated
cloud validation remain outstanding.

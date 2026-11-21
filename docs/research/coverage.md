# Capability coverage and open work

How far the integration covers each area of the MyQ app and API, and what
remains. "Covered" means code exists and offline tests exercise it; it does not
mean a real account or device has confirmed it. For what users get today, see
[devices](../devices.md) and [cameras](../cameras.md); for the protocol, see the
[API reference](../api/README.md).

| Area | Current coverage | Remaining work |
| --- | --- | --- |
| Password sign-in | Successful website login and validated app OAuth callback; browser/helper flow connected to HA setup | Resolve token-exchange rejection with genuine app verification and validate a real session |
| Required MFA | Successful email `Otp` form and hidden fields verified against HAR; browser renders real MFA | Verify SMS, enrollment, resend, error paths, and API token exchange |
| Browser verification | Detect `cf-mitigated: challenge`; keep browser open for normal verification | Confirm successful clearance in the user's session; raw HTTP cannot handle it |
| Session renewal | Serialized refresh at the app's effective lifetime, rotating-token persistence, the app's sign-out rules (only `invalid_grant`/`401.102`/`400` end the session), renew-and-resend after a renewable 401, revocation on entry deletion, same-account reauth; typed session-validation action checks identity/discovery | Verify real refresh lifetime, revocation and accepted session behavior |
| Android app verification | Provider chain traced; explicit `401.122` reported; probe and HA accept supplied proof with expiry checks. The one-time sign-in link option (no App Check header) was removed when its web page began saying web login is no longer supported | Obtain a genuine token and test acceptance; implement acquisition/renewal |
| Account/device discovery | All returned accounts (list cached for 6 hours, re-read after a failed device read), dynamic entities, offline/removal handling; roles from AccountAccess gate settings and actions as the app's screens do, with ownership from the account's `created_by` (the app never reads the permission lists) | Verify actual schemas, role combinations and API version; no claim of universal native UI parity |
| Garage state/control | Open/close with the app's precheck (online, monitor-only, PIN lockout, work-light error, virtual-opener sensor fault) and unattended flags | Physical validation, delayed cloud-state behavior, model-specific error responses |
| Stop/position | Not exposed | No established command contract |
| Opener worklight | Reported light state is a sensor | Verify controllable worklight capability and applicable route; standalone lamp endpoints are not assumed applicable |
| Standalone lamps | On/off light entity | Live validation |
| Battery/connectivity/cycles/faults | Sensors from returned fields | Confirm field types for the target opener |
| Lock mode ("Disable remotes") | Owner-only switch on Wi-Fi and 3C openers; readback from `in_vacation_mode` | Live validation on each opener model |
| Worklight motion/PIR settings | Residential-opener support discovery and combined switch; fresh support check before writes | Live model/role validation; no generic direct worklight on/off |
| Light timer / time-to-close | Timer select (90/150/210/270 seconds) and 0–99 minute time-to-close number; unattended-close permission enforced | Physical validation, model-specific behavior and complete account role mapping |
| Camera discovery | Camera device records and Tend ID conversion; opener cameras linked to their opener in the device registry (`attached_camera_serial_number`) | Validate attached/associated-camera topology for 87504R-267 |
| Snapshots | App's mode rule (`forceUseExisting` for battery cameras, else `forceRefresh`), fallback on any load failure, 6-second image timeout, 24-hour capture-time refresh (`tend-dttm`), 60-second cache, live-frame snapshot during a view, stale/in-flight image invalidation | Verify actual access and capture freshness |
| Camera on/off | Advertised capability only | Live validation |
| Microphone/motion/speaker | Switches and volume number with capability checks | Live validation of settings semantics and readback |
| Live video | Capability-gated KVS WebRTC, camera wake-up, TLS/Cxnet login/start/stop, temporary ICE credentials, one native session shared by up to four relayed viewers, signaling loss tolerated once media flows, HA media relay and lifecycle cleanup | Validate with target camera; legacy RTP, automatic reconnect and indefinite viewing are not implemented |
| Two-way audio | Permission-gated KVS microphone uplink, muted by default; HA viewer card with hold-to-talk, release/focus/timeout cleanup | Physical audio/echo behavior, product compatibility and legacy RTP talkback |
| Recorded clips and events | Account/camera media browser with thumbnails, local-time titles, durations and detection labels; Door events folder; door clips attached to Activity events and `get_device_history`; ownership checks; expiring HA-signed HLS proxy with MP4 fallback | Confirm real entitlement, ordering, retention, CDN URLs and player behavior; no deletion/favorites/full-file download |
| Camera status push | Read-only idle Tend monitor and active live sessions receive understood alias-scoped notifications; refresh REST state with polling fallback and bounded reconnect | Real-account delivery/compatibility; additional event schemas and KVS credential-push handling |
| Door/lock and full-account notifications | Native FCM registration/receiver paths traced separately | Genuine Google Play Services receiver/forwarder and HA delivery pipeline; registration tokens alone are insufficient |
| Camera advanced settings | Existing settings plus finite PTZ/zoom/stop and siren start/stop with permission gates and cleanup; resolution/FPS/quality readback | PTZ presets/patrol actuation, zones, AI policies and remaining coupled settings |
| Camera workflow schedules | Owner-only read/create/update/enable/delete; bounded first-configuration creation for eligible floodlight, patrol, privacy and voice-deterrence cameras; the app's overlap check before saving or enabling; lenient reads; readback | Other configuration/provisioning workflows, native service time-zone behavior and real-device validation |
| Other scenes/auto-secure | Legacy scenes, auto-secure and geofence interfaces indexed | Complete feature eligibility, CRUD, time-zone/ownership and conflict workflows |
| Alerts and snooze | Per-rule switches (full AlertDto written back, readback confirmed); account snooze and cancel actions; `get_alerts` | Creating/deleting rules, device-level snooze, live validation |
| Device history | Opt-in Activity event entity per door, lamp and lock (one read per account per full poll while enabled); paged `get_device_history` action | Unified v7 history, live validation of event mapping |
| Guests and access | Existing guest inventory, owner-only device-list changes limited to devices the app offers guests and preserving schedule restrictions, and confirmed removal | Deleting the guest's keypad PINs first, as the app does (the PIN service host was not recovered); new invitations, role changes, family/minor verification and expiry-policy workflows |
| Lock PINs | Legacy remote PIN inventory/create/update/delete for any role but guest, with sync completion checks (pending from the PUT answer, failed deletes reported); private-file input avoids raw PIN service arguments | Modern credential APIs, local Bluetooth, product validation and broader guest-pass policies |
| Opener accessories | Owner-only typed inventory (read as leniently as the app) and optional battery diagnostics; guarded rename (1–24 characters, unique on the opener), assignment and removal with readback | Pairing/learning, unsupported transmitter management and real-product validation |
| Onboarding/pairing | GDO, gateway, Wi-Fi and registration interfaces indexed | Provisioning workflows, physical-device interactions, calibration and model-specific validation |
| Firmware | Read-only update entities for locks/openers (`firmware_available`) and cameras (`hw.firmwareUpgradeAvail`) | Installing firmware is deliberately not offered |
| Dealer diagnostics | Interfaces indexed | Read/write capability and authorization research |
| Subscriptions/delivery/commerce | Interfaces indexed | Account/product workflows are not implemented |
| Remote locks | Wi-Fi/Seam and legacy gateway lock actions; legacy auto-relock/PIN management; owner-gated native `wifilockgateway` automatic-lock delay and calibrated door-sensor enable | Live validation, partner-lock configuration/credentials, Bluetooth transport, calibration and provisioning |
| Linked thermostats | Climate entity with case-insensitive enum reads, advertised modes/fan modes (off always offered), app-style running state, alert band attribute; writes follow the app's single mode action with fan, rounding, gap, missing-target rules and delayed readback; read-only presets/schedules action | Live validation, preset/schedule writes, partner linking and full account-role semantics |
| Gateways, bridges and unknown records | Device type and allowlisted returned diagnostics | Setup, firmware and topology-specific management remain incomplete |
| Pet doors and generic accessory types | Native taxonomy and returned diagnostics; supported opener accessories have dedicated management | Pet-door actuation and management for other unsupported accessory types |

The sign-in blocker is recorded in the [sign-in investigation](sign-in-investigation.md).

HTTP Retry-After cooldowns are scoped to service origins, survive setup retries
and reloads, and grow from one minute to an hour over repeated 429s without a
Retry-After. Reads the app makes only on its screens are cached: camera details 15
minutes, opener settings, alert rules and roles 30 minutes, the accounts list 6
hours; the poll interval is at least 60 seconds. Optional role, camera,
opener-settings or accessory polling failures are recorded without discarding
successfully discovered door state. These are integration behaviors,
not guarantees about MyQ's quota policy.

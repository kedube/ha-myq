# Services and routes

The hosts, API versions, request conventions and declared routes of MyQ Android
**5.324.0.1132442**. A declared route is not proof that the cloud accepts it:
authentication, account permissions, subscription, region and device
capabilities are separate requirements.

The API is a collection of services, not a single garage-door endpoint. MyQ
account/device APIs, hosted identity, Tend camera REST, Tend native signaling,
AWS KVS signaling, local provisioning and embedded third-party libraries have
different address construction and authentication rules. A path found in one
service must not be sent to another service merely because its name is similar.

## Catalog and audit scope

The [normalized API catalog](api_catalog.json) contains every route in the
existing [literal inventory](api_inventory.json), with source hashes, parameter
annotation summaries, declared body types and selected host-factory traces.

| Inventory measure | Result |
| --- | ---: |
| Retained Java files scanned by the existing extractor | 40,856 |
| HTTP declarations | 464 |
| Source files containing those declarations | 69 |
| Ordinary literal Retrofit annotations | 363 |
| Explicit `@HTTP` declarations, including DELETE bodies and PATCH | 9 |
| Embedded Tend/Seedonk SDK operation-table entries | 92 |
| Unique literal method/path pairs, without resolving hosts | 439 |
| GET / POST / PUT / DELETE / PATCH declarations | 167 / 136 / 105 / 55 / 1 |

Re-running `scripts/analyze_apk.py:inventory` on the retained source export
produced exactly the existing 464 records, with no added/removed records or
source-line drift. This establishes reproducibility within the extractor's
scope, not completeness of the app's network behavior. It misses dynamic URLs,
unresolved constants, nonliteral annotations, native sockets and code absent
from the partial decompilation. Duplicate entries can be overloads, different
hosts or library operations; they are deliberately retained.

The original inventory records the earlier JADX **1.5.6** run. This audit used
the retained **1.5.7** export, which had **558 decompilation errors**. The old
inventory is preserved as historical evidence rather than silently rewriting
its provenance. The catalog records both versions. No decompiled method bodies,
tokens, private device identifiers or captured account data are included.

Each catalog route has its original `source_file`, `line`, `source_type`,
`method`, `path`, and declaration kind. `interface_id` joins to the interface
table containing a SHA-256 of the reviewed source. `body_type` is a declared
Java DTO name, not a complete JSON schema. `parameter_annotations` distinguishes
path, query, form and header parameters; unresolved name constants remain
expressions. Maps, defaults, serializers and requiredness need caller/DTO review.
Every route remains `static_declaration` with `server_acceptance=unverified`.

Useful local queries:

```sh
jq '.summary' docs/api_catalog.json
jq '.routes[] | select(.source_file == "h01/a.java")' docs/api_catalog.json
jq '.interfaces[] | select(.host_binding.production_base_url == null)' docs/api_catalog.json
```

## Service topology and host selection

`assets/myq-config.json` supplies environment suffixes. The primary production
configuration includes `.myq-cloud.com`, `.tend-us.tendplatform.com`,
`.prod.tendplatform.com` for video calling, and `api.video.myq.com` for video
application services. Other packaged environment records include development,
preproduction and regional suffixes. Their presence does not establish which
one a particular account currently uses. No alternate environment was contacted.

`oy0/b.java`, `ta2/g.java` and the smaller service-host enums provide logical
subdomains. `ft0.i1` composes them with the selected environment; factories bind
interfaces to these hosts. The catalog resolves 47 interface bindings through
the inspected static factory chains. Remaining entries explicitly keep an
unresolved, dynamically configured, local or SDK-dependent base URL.

| Service | Primary production host or address rule | Evidence / purpose |
| --- | --- | --- |
| Identity | `partner-identity.myq-cloud.com` | `hp0/e.java`, `ip0/k.java`; hosted authorization and token operations |
| Accounts | `accounts.myq-cloud.com` | `wm0/a.java`, `dn0/a.java`, `dn0/g.java`; account/access records |
| Profiles, consent | `profiles.myq-cloud.com` | `y81/a.java`, `g91/a.java`; profile, email, consent and GDPR interfaces |
| General configuration | `general.myq-cloud.com` | Same factory; content, countries and time zones |
| Devices | `devices.myq-cloud.com` | `oy0/b.java`, `iz0/a.java`; account device discovery and device metadata |
| Device actions | `devices-action.myq-cloud.com` | `cv0/o.java`; door, modern-lock and thermostat action dispatch |
| Opener-specific APIs | `account-devices-gdo.myq-cloud.com` | `zz0/a.java`, `r11/a.java`; opener configuration, accessories and virtual doors |
| Lamps | `account-devices-lamp.myq-cloud.com` | `r01/a.java`; lamp state actions |
| Legacy locks | `devices-locks.myq-cloud.com` | `y21/a.java`; lock commands, settings and PIN interfaces |
| Gateways | `gateways.myq-cloud.com` | `rz0/a.java`; registration, learning and gateway management |
| Video keypads | `account-devices-vkp.myq-cloud.com` | `z11/a.java`; keypad registration/configuration |
| Device provisioning | `devices-provisioning.myq-cloud.com` | `z31/a.java`; device registration, preowned state and removal |
| Linked partner devices | `myq-partner-devices.myq-cloud.com` | `vy0/t.java`; partner linking and thermostat settings/presets/schedules |
| Guest access | `guestaccess.myq-cloud.com` | `wm0/a.java`, `mn0/*`, `un0/a.java`, `cy0/a.java`; guests, invitations, schedules and co-owner access |
| Credentials | `residential-credentials.myq-cloud.com` | `wt0/a.java`, `st0/a.java`; credential assignment, synchronization, learning and lockout settings |
| Alerts | `alerts.myq-cloud.com` | `bo0/a.java`, `io0/c.java`; alerts and snooze |
| Workflows | `workflows.myq-cloud.com` | `cv0/o.java`, `io0/c.java`, `kc1/a.java`; scenes, announcements, consolidated alerts and geofences |
| Account/device history | `accounthistory.myq-cloud.com` | `d61/a.java`, `l61/z.java`, `t61/a.java`; unified and device history |
| Push registration | `notifications.myq-cloud.com` | `m91/a.java`, `s91/a.java`; phone registration/unregistration |
| Delivery integration | `delivery-integration.myq-cloud.com` | `oy0/a.java`, `l21/a.java`; delivery-linked features |
| Feature gates / region configuration | `feature-gate.myq-cloud.com`, `region-configurations.myq-cloud.com` | `v41/c.java`, `j51/d.java`; remote feature and environment selection data |
| Camera REST | `media.hubs.tend-us.tendplatform.com` | `nt0/d.java`, `nt0/e.java`; details/settings/media. The native endpoint selector has an alternate Tend web-host branch. |
| Recorded-event media migration | `events-media.prod.video.myq.com` | `nt0/b.java`, `nt0/c.java`, `ft0/i1.java`; feature-flagged rewrite of event media URLs. Integration accepts advertised event-scoped resources at this origin without forcing the flag. |
| Video preferences, smart search, routines and recap | `api.video.myq.com` | `d61/b.java`, `d61/c.java`, `d61/d.java`, `l61/z.java`; host comes from `myqVideoServiceBasedHostname` |
| Calling/notification application | `api.prod.tendplatform.com` | `VideoCallingServiceHost.HOST="api"` plus packaged calling suffix; `RemoteApiModule` also supports injected base URLs |
| Local Wi-Fi provisioning | Network-helper address; HTTP or HTTPS selected by accessory state | `p31/b.java`; not a cloud endpoint |
| Legacy persistence APIs | `environment.getPersistenceHostname()` | `it0/d.java`; cannot safely substitute the modern devices host |
| Dealer locator | `code.metalocator.com` | Explicit third-party base in `qe2/b.java` |

The legacy core host enum `ta2/g.java` additionally names `devices-camera`,
`pins`, `myq-partner`, `gdoclassifier`, `history`, `billing`, `delivery`,
`az-oauth`, `scheduling`, `diagnostics` and `ecommerce`. `y92/b.java` passes these
through `nc1/FrameworkConfiguration` into factories in `oc1/m.java` and `pc1/*`.
Those are useful service identity evidence. The catalog leaves some individual
legacy interface bases unresolved rather than treating an enum name as a
completed per-route binding. `myq-partner` and `myq-partner-devices`, and
`history` and `accounthistory`, are distinct names.

## Request authentication, metadata and transport behavior

Native authenticated JSON services use the access token with its token type,
normally `Authorization: Bearer …`. `ft0/i0.java` constructs authorization and
adds configured metadata. `y92/b.java:s` supplies `BrandId: 1` and the native
`App-Version: 5.324.0.1132442`. The integration's own User-Agent is an integration
identifier, not an observed app fingerprint.

OAuth code exchange is form-encoded and has the App Check path described in
[authentication](authentication.md). `ip0/s.java` exposes multiple
`/connect/token` declarations: ordinary exchange, exchange with
`Firebase-AppCheck-Token`, and a refresh-tagged method with an internal
`IsRefresh: true` marker. Interceptors complete the refresh request. A marker in
an internal Retrofit declaration is not evidence that a client should copy that
marker directly as its entire wire contract.

Tend REST adds `AntiCSRF: AntiCSRF` through `ot0/a.java`; authentication and
media behavior are covered in [video](video.md). Signed KVS WebSocket URLs
and temporary TURN credentials have their own authorization. Forwarding MyQ
bearer tokens or cookies to those URLs would not reproduce the native contract.
The local accessory provisioning factory adds `Wifi-Provision: true` and uses
the accessory/network-helper-selected local address.

API version strings are service-specific. Established action calls use `v1.0`,
ordinary accounts/devices and several device services use `v6.0`, but modern
lock configuration declares literal **`/api/v1/…`**, QLink learning declares
**`v5.2`**, commerce includes `v2`/`v2.1`, and video application APIs have their
own version placement. The catalog preserves literal casing, leading slashes
and placeholders. Do not normalize `Accounts` to `accounts` or insert `v6.0`
everywhere without caller evidence.

The Home Assistant client adds implementation protections: HTTPS validation,
bounded responses, no automatic replay of an uncertain mutation, one in-flight
mutation per device, and post-command state refresh. Any request answered with a
renewable 401 (body code `401.102` or `401`, or none) renews the access token and
is sent once more, as the app's authenticator does. The other protections are
integration choices. In particular, HTTP success does not
prove a door moved, a lock settled or a physical setting was applied.
The integration scopes Retry-After cooldowns to origins, isolates optional polling
failures from device discovery, and adds readback completion for management
writes. Session validation checks profile identity and discovery using the
configured tokens; it does not manufacture app verification or prove hardware
operation.

## Established discovery and action contracts

These routes have been traced beyond declarations and implemented with local
synthetic tests. Server acceptance and physical outcomes are still unverified.
`{account}`, `{serial}` and other identifiers below are placeholders.

| Purpose | Method, host family and route | Body / important result |
| --- | --- | --- |
| Profile identity | GET profiles `/api/v6.0/my/profile` | Profile includes `user_id`; integration uses this for stable account identity |
| Accounts | GET accounts `/api/v6.0/accounts` | `accounts` list with account identifiers/names |
| Account permissions | GET accounts `/api/v6.0/accountAccess` | Typed roles and explicit resource grants; missing/unknown grants do not authorize added management/media controls |
| Devices | GET devices `/api/v6.0/Accounts/{account}/Devices` | `items`, `count`, `href`; no paging parameter recovered for this list |
| Door/gate action | PUT devices-action `/api/v1.0/accounts/{account}/devices/{serial}` | `{"action":"open","trigger":"myq_an","device_family":"garagedoor"}`; `close` is the other established action |
| Modern lock | Same action route | Only `{"action":"lock"}` or `{"action":"unlock"}`; door trigger/family are omitted |
| Legacy remote lock | POST devices-locks `/api/v6.0/accounts/{account}/devices/locks/{serial}/{lock|unlock}?requestRemote=true` | No body; the local/Bluetooth branch is a separate operation |
| Lamp | PUT account-devices-lamp `/api/v6.0/accounts/{account}/lamps/{serial}/{on|off}` | No guessed brightness/color payload |
| Thermostat mode | Same action route | `action` is off/heat/cool/heat_cool, `device_family="hvacs"`, `temperature_unit`, and only applicable heating/cooling targets |
| Thermostat fan | Same action route | `{"action":"set_fan_mode","device_family":"hvacs","fan_mode":"auto"}` or another advertised fan mode |
| Opener light timer | GET/PUT GDO `/api/v6.0/accounts/{account}/door_openers/{serial}/lighttimer` | `light_timer_setting`: 90, 150, 210 or 270 seconds |
| Opener time-to-close | GET/PUT same prefix plus `timetoclose` | `time_to_close_setting` in seconds; native UI permits off or 1–99 minutes |
| Motion-activated opener light | GET/PUT same prefix plus `worklight_settings` | `enable_pir_control` and `enable_motion_wl`; native combined switch writes both |
| Camera details | GET Tend `/cxs/api/devices/{id}` | Separate `cap`, `settings`, `status`, alias and hardware/power information |
| Camera settings | PUT Tend `/cxs/api/devices/{id}/settings` | Typed partial sections; each setting depends on capability/current-state evidence |
| Camera snapshot | GET Tend `/cxs/api/devices/{id}/cam/recentImage.jpg?size=HD` with `forceUseExisting=true` (battery cameras) or `forceRefresh=true` | `wy/o.java` chooses the mode; freshness is not established by the path alone |
| Camera wake-up | POST Tend `/cxs/api/devices/{id}/do/wakeup` | Empty object; poll power state before live startup when required |
| Recorded events | GET Tend `/cxs/api/events` | Account/camera filters, `v=2`, date-boundary pagination and finalized-state checks |
| Recorded media | GET Tend `/cxs/api/events/mediaDownloadInfo`, then advertised event `video.m3u8` resource | Authenticated URL discovery followed by an expiring HA HLS proxy; cloud credentials are not forwarded to CDN hosts |
| Native Wi-Fi lock configuration | PUT devices-action `/api/v1/accounts/{account}/devices/{serial}/lockconfiguration` | Native `wifilockgateway` only: `automatic_lock_state` with an optional delay, or calibrated `door_sensor_state`; commands use `Enable`/`Disable`, readback uses lowercase state |

Source chains and permission gates are documented in [device coverage](../research/device-families.md),
[garage evidence](../research/devices-garage-evidence.md), [lock evidence](../research/devices-lock-evidence.md)
and [camera evidence](../research/devices-camera-evidence.md). New complete local workflows
are detailed in [management implementation](management.md),
[recordings implementation](recordings.md) and
[live controls/talkback](live-controls.md). Live PTZ/siren use
typed Tend action-411 commands, not a speculative HTTP endpoint. A shared
action URL does not make its device-family payloads interchangeable.

## Other application API surfaces

The following declarations demonstrate the application's broader surface. The
status column distinguishes implemented workflows from research-only
declarations. DTOs, role checks, completion conditions and error/recovery
behavior outside the linked implementation ledgers remain partly traced.

| Area | Interface sources | Established declaration surface / unresolved behavior |
| --- | --- | --- |
| Account/profile management | `an0/a.java`, `an0/b.java`, `b91/*`, `nd1/f.java`, `ud1/a.java` | AccountAccess read/grant enforcement implemented. Profile/email/consent, account creation/deletion and complete lifecycle remain separate research surfaces. |
| Guests and invitations | `in0/*`, `rn0/*`, `zx0/a.java` | Existing guest inventory, guarded device-access updates preserving schedule restrictions, and verified removal implemented. New invitations, role upgrades and family/minor policy remain incomplete. |
| Credential management | `xt0/a.java`, `td1/a.java`, `e11/a.java` | Legacy remote lock PIN CRUD/sync implemented with private-file input and no numeric PIN in responses. Modern credential/guest-pass, learning and lockout workflows remain incomplete. |
| General device management | `gz0/a.java`, `v01/b.java` | Device records, transmitters, sounds and metadata updates; legacy route separate from modern discovery. |
| Opener/accessory setup | `wz0/a.java`, `m11/*`, `nd1/i.java` | Typed inventory and supported assignment/rename/removal implemented with association checks/readback. Pairing, learning, reorder and virtual-door provisioning remain incomplete. |
| Gateway setup | `qz0/a.java` | Programmable devices, sensor learn/start/stop/status/connected, registration, preowned account, rename/remove and extended warranty. |
| Wi-Fi/device registration | `o31/a.java`, `w31/a.java`, `w11/a.java` | Local `jstart`, `jscan_results`, `jconfig_save`, `jconnect_serial`; cloud registration, preowned checks and keypad setup. Local HTTP/HTTPS is a distinct transport. |
| Legacy lock settings | `e11/a.java` | Owner-gated auto-relock and permitted PIN CRUD implemented with remote completion polling. Firmware/orientation/local encrypted Bluetooth workflows remain separate. |
| Modern lock configuration | `bz0/e.java`, `r80/h.java`, `sb0/f2.java` | Native `wifilockgateway` automatic-lock delay/Off and calibrated door-sensor enable implemented through literal `/api/v1/.../lockconfiguration`, with fresh owner grants and readback. Partner-lock configuration, calibration and chime setup remain separate. |
| Partner thermostat linking/configuration | `h01/a.java` | Partner webviews/connect, linked devices, temperature units, threshold, presets and schedules. Mode actions use devices-action instead. |
| Scenes and schedules | `nd1/j.java`, `ly0/j.java`, `c21/WorkflowSceneCreateDTO.java` | Camera feature schedule read/create/update/enable/delete and bounded first-configuration creation implemented with model/capability gates, fresh grants and readback. Broader provisioning and legacy/general scenes remain incomplete; real service time-zone behavior unverified. |
| Auto-secure/geofences | `nd1/b.java`, `hc1/a.java` | Auto-secure create/read/update, schedules, snooze and eligibility; geofence workflows. A schedule can cause later physical activity, so declaration alone is not implementation. |
| Alerts and snooze | `fo0/a.java`, `fo0/b.java`, `fo0/c.java` | Alert CRUD, linked alerts, notification/reminder actions, account/device snooze and eligibility; consolidated workflow events. |
| Device/unified history | `h61/d.java`, `r61/a.java` | Device history GET supports `devices` and `continue` query fields; unified history search/filters, favorites and deletion. Pagination differs from device discovery. |
| Motion events/media | `h61/i.java`, `vf1/g.java`, SDK table | Scoped event browsing/pagination, verified HLS resource resolution and authenticated HA playback proxy implemented. Real entitlement, ordering, retention and CDN compatibility remain unverified; feedback/deletion are not exposed. |
| Smart video search/preferences | `h61/c.java`, `h61/f.java`, `h61/h.java` | Account/device activation, preindex status, longitudinal-data sharing, consent and user/account settings. These are separate from camera capture controls. |
| Routines/recap | `h61/g.java`, `h61/e.java` | Household routine create/patch/delete/confirm/reject/enable/disable and wrapped nudges; no complete integration workflow yet. |
| Calling/push | `NotificationAppRemoteApi.java`, `WebRtcAppRemoteApi.java`, `p91/a.java` | Calling and full-account door/lock FCM delivery remain unimplemented and need a genuine receiver/forwarder. Separate read-only idle Tend monitoring and active live-session notifications refresh camera REST state with polling fallback. |
| Camera associations | `rd1/a.java` | Camera/GDO association create/delete, sync-device-list and camera location. A camera and its opener/lock may be separate records. |
| Face/identity features | `le1/a.java`, `vf1/g.java`, SDK table | Account-scoped face identity list/create/update/delete and related media declarations. UI availability, consent, matching behavior and biometric processing were not established merely from these routes. |
| Firmware | `e31/a.java`, SDK table, lock/provisioning APIs | Firmware-related reads/updates and status declarations. Safe update prerequisites, transfer, progress and recovery have not been completed. |
| Dealer diagnostics | `pd1/a.java`, `qe2/a.java` | Dealer lookup/association/access, health reports and fault alerts. Not a general authorization to grant dealer access or change device firmware. |
| Delivery and partners | `i21/a.java`, `nd1/a.java`, `nd1/c.java`, `nd1/g.java`, `nd1/e.java` | Delivery settings/locations, partner lookup and OAuth/linking declarations. Product/account-specific authorization remains unverified. |
| Subscriptions and feedback | `vd1/a.java`, `vd1/b.java` | Camera/account subscriptions, information requests and feedback; billing eligibility and purchase flows remain separate. |
| Commerce | `sd1/a.java`, `sd1/b.java` | Guest/signed-in product pricing, shipping, tax and Google Pay order placement. No checkout or payment requests were performed. |
| Feature/region configuration | `w41/a.java`, `k51/a.java` | Remote feature gates and region configuration; bundled enum presence does not establish feature rollout. |
| Activity logging | `vf1/b.java` | Tend activity-log service; no claim that all events or payloads were dynamically observed. |

For exact individual paths, query `api_catalog.json` by `source_file` or `source_type`.
The complete 69-interface index below is generated from the audited catalog.

## Complete interface index

<!-- Interface index generated from api_catalog.json; counts are declarations. -->

| Source file | Original source type | Declarations |
| --- | --- | ---: |
| `an0/a.java` | AccountAccessApi.kt | 1 |
| `an0/b.java` | AccountApi.kt | 3 |
| `b91/a.java` | ConsentApi.kt | 5 |
| `b91/b.java` | GDPRApi.kt | 2 |
| `b91/c.java` | GeneralServicesApi.kt | 4 |
| `b91/d.java` | ProfileServicesApi.kt | 5 |
| `bz0/e.java` | DevicesActionV1Api.kt | 3 |
| `com/chamberlaingroup/video/webrtc/network/NotificationAppRemoteApi.java` | NotificationAppRemoteApi.kt | 2 |
| `com/chamberlaingroup/video/webrtc/network/WebRtcAppRemoteApi.java` | WebRtcAppRemoteApi.kt | 1 |
| `com/seedonk/mobilesdk/RestApi.java` | Tend/Seedonk RestApi | 92 |
| `e11/a.java` | LockServiceV6Api.kt | 21 |
| `e31/a.java` | TendDeviceFirmwareApi.kt | 2 |
| `f41/a.java` | TendDeviceApi.kt | 12 |
| `fo0/a.java` | AlertsApi.kt | 12 |
| `fo0/b.java` | ConsolidatedAlertsApi.kt | 2 |
| `fo0/c.java` | DeviceSnoozeApi.kt | 5 |
| `gz0/a.java` | DeviceServiceCustom.kt | 6 |
| `h01/a.java` | PartnerDevicesV1Api.kt | 13 |
| `h61/c.java` | AnalyticsSharingApi.kt | 9 |
| `h61/d.java` | DeviceHistoryApi.kt | 2 |
| `h61/e.java` | NudgeApi.kt | 1 |
| `h61/f.java` | PreferencesApi.kt | 11 |
| `h61/g.java` | RoutinesApi.kt | 8 |
| `h61/h.java` | SmartSearchApi.kt | 6 |
| `h61/i.java` | TendMotionEventsApi.kt | 1 |
| `hc1/a.java` | GeofenceApi.kt | 4 |
| `i21/a.java` | DeliveryIntegrationApi.kt | 9 |
| `in0/a.java` | GuestAccessScheduleApi.kt | 1 |
| `in0/b.java` | GuestApi.kt | 7 |
| `ip0/s.java` | MyQAuthServiceRequests.kt | 6 |
| `k51/a.java` | RegionConfigApi.kt | 1 |
| `le1/a.java` | TendCloudServiceV5.kt | 7 |
| `ly0/j.java` | WorkflowV1Api.kt | 14 |
| `m11/a.java` | VGDOServiceApiV5.kt | 1 |
| `m11/b.java` | VGDOServiceApiV6.kt | 5 |
| `nd1/a.java` | AmazonServiceRequests.kt | 1 |
| `nd1/b.java` | AutoSecureServiceApi.kt | 9 |
| `nd1/c.java` | DeliveryServiceV5.java | 3 |
| `nd1/d.java` | DeviceDetectionApi.kt | 4 |
| `nd1/e.java` | IdentitySetupRequests.kt | 3 |
| `nd1/f.java` | ManageAccountServiceV6.kt | 1 |
| `nd1/g.java` | PartnerServiceV6.kt | 2 |
| `nd1/i.java` | QLinkServiceRequests.kt | 2 |
| `nd1/j.java` | SceneServiceApi.kt | 4 |
| `o31/a.java` | WifiProvisioningApi.kt | 5 |
| `p01/a.java` | LampApi.kt | 4 |
| `p91/a.java` | PushNotificationsApi.kt | 2 |
| `pd1/a.java` | DealerDiagnosticsServiceV6Api.kt | 16 |
| `qe2/a.java` | MetaLocatorApi.kt | 1 |
| `qz0/a.java` | GatewayServiceV6Api.kt | 13 |
| `r61/a.java` | UnifiedHistoryApi.kt | 6 |
| `rd1/a.java` | CameraServiceV6Api.kt | 5 |
| `rn0/a.java` | ActiveAccountInvitationApi.kt | 3 |
| `rn0/b.java` | InvitationApi.kt | 5 |
| `sd1/a.java` | EcommerceGuestServiceV6Api.kt | 4 |
| `sd1/b.java` | EcommerceServiceV6Api.kt | 5 |
| `td1/a.java` | UserDevicePinV6ServiceApi.kt | 11 |
| `ud1/a.java` | UserCreationServiceV6Api.kt | 1 |
| `v01/b.java` | LegacyDeviceServiceApi.kt | 1 |
| `vd1/a.java` | BetaFeedbackServiceV6Api.kt | 3 |
| `vd1/b.java` | SubscriptionServiceV6Api.kt | 5 |
| `vf1/b.java` | ActivityLogService.kt | 1 |
| `vf1/g.java` | TendCloudServiceV5.kt | 6 |
| `w11/a.java` | VkpDeviceRegistrationApi.kt | 8 |
| `w31/a.java` | DeviceRegistrationApi.kt | 5 |
| `w41/a.java` | FeatureGateApi.kt | 1 |
| `wz0/a.java` | GdoApi.kt | 23 |
| `xt0/a.java` | CredentialsApi.kt | 16 |
| `zx0/a.java` | DeviceCoownerAccessApi.kt | 1 |

## Limits

The API catalog is intentionally not presented as OpenAPI: it does not establish
complete schemas, server-side requiredness, status codes, permissions or security
schemes for all routes. A route's declaration does not prove a reachable UI,
enabled product feature, successful request or complete protocol. Native
Cxnet/KVS/Bluetooth exchanges are not counted among its 464 HTTP declarations.

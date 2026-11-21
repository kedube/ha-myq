# Camera capability and settings evidence

This records the additional camera work from the MyQ Android 5.324.0.1132442 investigation on 2026-11-20. It complements the existing snapshot/live-video investigation. The observations below are static JADX evidence from a local export, kept outside the repository; source paths in the tables are relative to the export's sources directory. This pass reused the already decompiled APK. These source locations are not runtime observations.

## Device capability model

Camera type alone does not establish that a particular control exists. `g41/TendDeviceCapabilitiesDto.java` separates `cam`, `mic`, `spk`, `lightList`, `siren`, `statusLed`, `doorbell`, `chime`, `pwr`, `wakeOnMotionCap`, `autoSecure`, `wifiExtend`, `gateway`, `dvr`, `event`, and `util`. `g41/TendSettings.java` supplies corresponding settings, while `g41/TendDeviceStatus.java` supplies connectivity, power, firmware-upgrade and camera status.

This applies to integrated garage cameras, standalone indoor/outdoor cameras, PTZ cameras, floodlight/worklight cameras, video doorbells, video keypads, and camera-equipped locks/pet doors when they return these capabilities. It is not a claim that every family supports every setting. In particular, the worklight-camera light array is distinct from a garage opener's worklight command.

The integration creates these additional entities only when both an explicit capability and a decodable current setting are present. It reads fresh camera details again before writing. Mutations require an online registered camera and current Tend `status.online: true`. PTZ auto-tracking also requires `settings.cam.enabled: true`. Configuration such as image orientation or LED state remains editable while privacy is enabled, since those settings do not start video capture.

## Request and caller chain

`j41/a.java:16` constructs the `f41/a` Tend API with the media host provider `nt0/e`. `f41/b.java` installs the AntiCSRF interceptor (`ot0/a`). Existing integration transport supplies the authenticated bearer token.

`e41/n.java:G` maps an explicit `rv0/b` settings payload through `i41/e.java:b`; its `H` callback calls `f41/a.java:b`. That Retrofit method is **PUT `cxs/api/devices/{deviceId}/settings`**. `e41/n.java:E` independently constructs the typed auto-tracking body, and its `F` callback calls `f41/a.java:k` at the same route. Camera details are **GET `cxs/api/devices/{deviceId}`** (`f41/a.java:l`). The integration uses its existing camera-ID extraction and media host.

`i41/e.java` builds nullable, partial settings DTOs from explicit payload sections. `i41/c.java:b` shows which camera fields the modern update path actually emits; resolution, frame rate and quality are omitted from that write mapper. `mz0/c.java` wakes the camera before payloads that require hardware online (`requiresHardwareDeviceOnline`, true by default) when the camera is not AVAILABLE. The integration sends the same `do/wakeup` request and waits for full power before writing; a camera whose `status.online` is not true is still rejected.

## Implemented additions

All bodies below are allowlisted settings PUT payloads. The API shares the existing video settings lock across a fresh read and write, then uses the client's per-device mutation helper. An ambiguous transport result raises `CommandUncertain`; it is never automatically replayed.

| Home Assistant entity | Required returned evidence | PUT body and native source |
| --- | --- | --- |
| Motion sensitivity number | `cap.cam.motion`, integer `motionSensMin`/`motionSensMax`, valid current `settings.cam.motionSens` | `{"cam":{"motionSens":N}}`; `i41/c.java:b`, `g41/CamCapabilitiesDto.java` |
| Siren volume number | `cap.siren.minVol`/`maxVol`, valid `settings.siren.vol` | `{"siren":{"vol":N}}`; `i41/k0.java:b`. This adjusts volume; it does not sound the siren. |
| Image orientation select | Supported current rotation and `cap.cam.rotationList` | `{"cam":{"rotation":value}}`; `i41/c.java:b`, `i41/d.java`, `g41/h0.java` and SDK `com/seedonk/mobilesdk/CamSettings.java:setRotation`. Values are normal=1, mirror=2, mirror_flip=3, flip=4. |
| Night vision select | `cap.cam.ir`, supported current `settings.cam.ir` | `{"cam":{"ir":value}}`; `i41/c.java:b`, `i41/y.java`, `g41/y.java` and SDK `CamSettings.java:setIrMode`. Values are off=0, on=1, auto=2. |
| Status LED switch | `cap.statusLed.ledOnOff`, current `ON`/`OFF` | `{"statusLed":{"status":"ON"}}` or `OFF`; `i41/p0.java:b` |
| Doorbell chime switch | Current type included in `cap.chime.typeList`, typed enabled/duration settings | `{"chime":{"enabled":bool,"type":existing,"duration":existing}}`; `i41/g.java:b`, `g41/h.java`. Existing type/duration are preserved. |
| PTZ auto-tracking switch | `cap.cam.ptz.autoTracking.enabled`, typed enabled/objects settings, camera enabled | `{"cam":{"ptz":{"autoTracking":{"enabled":bool,"objects":existing}}}}`; `e41/n.java:E`, `h41/TendUpdateAutoTrackingBody.java`, `CamPtzAutoTrackingDto.java`, `PtzAutoTrackingDto.java`, `AutoTrackingSettingsDto.java` |
| One light per returned camera light-list entry | Matching capability/settings arrays; advertised `on`/`off`; valid current switch state, intensity, motion trigger and trigger duration | `{"lightList":[...]}`; `i41/u.java:c`, `g41/LightSettingsDto.java`, `g41/LightCapabilitiesDto.java`, `px0/i.java`. Preserve all positions and the four fields the native mapper emits. Change only selected light's switch state and optional intensity. |
| Video resolution/frame-rate/quality sensors | Typed current `settings.cam.res`/`fps`/`quality` | Read-only; `i41/c.java:a`, `g41/Cam.java`. Empty, negative, non-finite and malformed sensor values are omitted. |

Orientation and IR preserve the observed server representation: integer values remain integers; supported named DTO enum values remain uppercase enum strings. The SDK explicitly uses numeric values; the modern DTOs use enums. This accommodates the two observed code representations without coercing an unknown server shape. Runtime acceptance of either representation remains unverified.

Light brightness is enabled only when `setIntensity` is true and the native integer range/current value are valid. Home Assistant brightness 1–255 is mapped to that native range; brightness zero turns the light off. The native `program` switch state does not establish current illumination, so the entity reports unknown on/off state for it. Light schedules, presets, motion-trigger settings and trigger duration are preserved rather than exposed as new controls.

Existing privacy, microphone, motion-detection and speaker-volume entities remain implemented through the original video module. They are not duplicated here.

## Observed but not implemented

| Feature | Evidence and remaining boundary |
| --- | --- |
| Manual PTZ movement, zoom and stop | `g81/a.java:SetPtzCommand` builds `set-ptz`; `p070rd0/e.java:U` sends it through `g81/e` with viewer/device aliases and correlation data. `g81/f.java:c` gates the session and calls `ConnectionManager.sendCxnetCommand`. Requires the authenticated Tend IM session, command responses, movement lifecycle, keepalive/watchdog and stop behavior. No speculative REST command has been added. |
| Siren activation/deactivation | `g81/a.java:SetSirenCommand` builds `trigger-siren-action`; `fh0/a.java` constructs and dispatches it through the same live command channel. `l81/SetSirenCommandPayload.java` contains viewer alias and action list. Volume settings alone do not implement siren activation. |
| PTZ presets, patrol and drift | `f41/a.java:f,h,j,i` and corresponding `h41` typed bodies show settings routes and preset thumbnails. Complete UI semantics, state transitions and movement behavior were not validated in this scope. |
| Talkback/two-way audio | Existing live-stream support does not establish an upstream microphone track or its device/session negotiation. No transmit-audio support is claimed. |
| Recorded video, history and subscription features | Capability/settings DTOs distinguish motion/full/local-DVR recording and services. This pass did not complete the entitlement, listing, retention, playback and deletion flows; snapshots/live streams do not imply recordings support. |
| Resolution, FPS, quality and codec writes | Capability lists and SDK setters exist, but modern `i41/c.java:b` does not emit these fields. Exposed as readback only where implemented above; a setter name alone is insufficient evidence for a supported mutation. |
| Detection/activity zones, AI object policies, voice deterrence, enhanced night vision and mounting | Native payload and capability DTOs expose these concepts. Their geometry, enums, coupled fields, entitlement and device-state rules were not fully traced. Existing auto-tracking object selection is preserved. |
| Chime type/duration, lighting schedules/presets and motion-trigger duration | DTOs and current values are observed; configuration dependencies and legal ranges are not fully established. Only chime enable and light on/off/brightness are exposed. |
| Firmware upgrade | `TendDeviceStatus` exposes upgrade-in-progress and power data includes firmware-upgrade battery threshold. These fields do not establish a complete, safe firmware mutation procedure. |
| Video-keypad guest PIN/access management | Video settings establish the camera portion only. PIN enrollment, invitation and access policy operations are separate account/access workflows and are not inferred from camera support. |

Server-side sharing/role rules and actual device behavior remain unverified. Capability checks are not a substitute for the service's authorization checks. No cloud requests, physical-device commands, firmware changes or PIN operations were performed during this pass.

## Verification

`tests/test_camera_settings.py` uses synthetic transport responses. Its 32 tests cover exact payloads/auth headers, native limits and enum representation, capability rechecks, offline/private-camera rejection, positional light preservation, invalid inputs, uncertain-write non-replay, entity creation and readback. Ruff check and formatting pass. These tests establish local implementation behavior, not successful authentication or live device compatibility.

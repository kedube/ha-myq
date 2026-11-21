# Live controls

PTZ, siren and microphone commands on an open live session, how the integration
gates and stops them, its live view card, and the idle camera status connection,
from MyQ Android 5.324.0.1132442. No physical camera has been used. The live
session itself is described in [cameras and video](video.md).

## Native commands and gates

| Feature | Observed app path | Integration behavior |
| --- | --- | --- |
| Manual pan/tilt | `rd0/e.java:J`, `k81/a.java`, `g81/a.java:SetPtzCommand`, `g81/c.java` | `set-ptz\|viewer\|P\|-1\|` left, `P\|1` right, `T\|1` up, `T\|-1` down. Finite steps only; no continuous `C`/`-C` motion. |
| Zoom | Same PTZ repository | `Z\|1` in, `Z\|0` out. Only exposed when `cap.cam.ptz` is present, matching `xz/CameraPtzCapabilities.java`. |
| Stop | `rd0/e.java:t/u`, `k81/a.java` | `set-ptz\|viewer\|S\|0\|`; sent by explicit stop, a one-second local watchdog, and cleanup of owned motion. |
| Siren | `fh0/a.java:i`, `l81/c.java`, `l81/d.java`, `g81/a.java:SetSirenCommand` | `trigger-siren-action\|viewer\|31\|01\|` ON and `31\|02` OFF. Explicit buttons start for at most 10 seconds or stop; transport helper accepts a bounded 1–60 second duration. |
| Microphone | `f81/b0.java:g/l`, `WebRtcPlayer.java:enableLocalAudio`, `PeerConnectionModel.java:addLocalAudioTrack` | Native mode 12 adds a muted local WebRTC audio track, then toggles it for talkback. The bridge negotiates an audio uplink only when permitted, forwards silence by default, and enables it for at most 30 seconds on an explicit action. |
| Status push | `com/seedonk/im/CommandHandler.java:parseCmdData`, `h81/a.java:b` | Alias-scoped `status`, `device-info`, `spydonk-device-info`, and `ptz-status` messages invalidate camera details. Live sessions and the read-only idle monitor receive these pushes; they do not replace REST state with inferred values. |

Commands use the existing action-411 Cxnet routing (`CX_DSTID` = camera alias),
the authenticated viewer alias, and a fresh correlation ID. Each start/move is
submitted once. A completed socket write is not represented as a confirmed
physical action. Siren buttons therefore do not invent a siren on/off entity state.
Device acknowledgements and camera errors can still arrive asynchronously.

The button helper re-reads the account role and camera details immediately
before activation. Like the app, it allows any known account role. It requires Tend
`status.online=true`, `settings.cam.enabled=true`, no firmware upgrade, and the
specific advertised capability. Failed, unknown, pending, or absent-success PTZ
calibration values are rejected when reported. Missing calibration status alone
does not invent a failure. The siren requires `cap.siren.activatable=true`; the
microphone requires `cap.spk.talkback=true`. Losing that role leaves an open
live view running with its controls disabled; stop/mute remains possible. Per-camera locks reject overlapping operations. Cleanup
stops owned motion/siren before closing Tend, and always stops microphone tracks.
Shutdown is held by a single retained task and shielded from caller cancellation.
Concurrent or later close calls await that same cleanup, so cancelling a viewer
cannot abandon the stop commands after their watchdogs have been cancelled.

## Home Assistant card

The integration ships `www/myq-live-card.js` and loads it into the frontend
automatically from a versioned URL; a manual `/myq/live-card.js`
dashboard resource also still works. The card editor provides camera and
microphone button entity selectors; it never guesses entity IDs. An example configuration is:

```yaml
type: custom:myq-live-card
entity: camera.your_camera
microphone_on: button.your_camera_enable_microphone_for_30_seconds
microphone_off: button.your_camera_mute_microphone
```

Select the actual entity IDs created in your installation. **Start view** opens
receive-only video/audio. **Start with microphone** requests browser microphone
permission and opens a muted microphone track. **Hold to talk** first requests
the integration's fresh authorization check, then enables the local microphone.
Releasing the button, losing window focus, hiding the page, hitting the timeout,
or closing the view immediately disables local audio. A delayed authorization
response after release cannot enable the track. Keyboard Space/Enter are supported.
Microphone access requires the browser's normal secure-context permission.

The card uses Home Assistant's installed native `camera/webrtc/get_client_config`,
`camera/webrtc/offer` subscription, and `camera/webrtc/candidate` messages, as
defined in HA 2026.2.3 `components/camera/webrtc.py`. Unsubscribing closes the
server-owned camera session. MyQ tokens, signed KVS URLs, and camera ICE secrets
remain on the server side. The ordinary HA camera card remains usable for
receive-only viewing; it need not offer a microphone track.

## Limits

These commands have been tested only against local peers, not physical cameras.
Full-account FCM delivery is separate from Tend camera status. Legacy
Seedonk RTP/UDP talkback is separate from the implemented KVS WebRTC path.
The integration has no live device evidence for siren acknowledgements, every
PTZ firmware variant, or browser/camera echo cancellation characteristics.

## Read-only idle camera status

The native login itself is sufficient to receive camera status. Specifically,
`ContactManager.java:onLoggedIn` parses the login's `self-list`/`buddy-list`, then
starts `ConnectionManager.startRefreshWait` and the 35-second keepalive before a
live-view request. `ConnectionManager.java:run` calls `eg1/b.java:t` with
`refresh_wait`. In the TLS branch, that method reads the existing socket through
`f63189w.o()`; it does not send a subscribe command or request live video.

`TendStatusSession` implements this login/read/keepalive/logout path and filters
invalidations to aliases already obtained from authenticated camera discovery.
It cannot send camera controls. `CameraStatusMonitor` owns its task and socket,
starts when known camera aliases appear, closes when they disappear, and pauses
before any live-view session opens. Live-view cleanup resumes the idle monitor.
This follows the app's shared connection ownership without starting hidden live
video. Changes trigger coalesced authenticated reads of camera/device state.
The feed is supplemental; normal polling remains authoritative.

Every reconnect obtains the current OAuth access token. Transient failures back
off from 60 to at most 900 seconds, honoring longer explicit quota delays.
Authentication failure ends automatic retries and reports the error to Home
Assistant. A session-level refusal (kick-out, more than 10 consecutive error
replies, refused login) backs off to the 900-second ceiling, then reconnects. Unload cancels the task and closes the TLS socket. Synthetic and
real local TLS tests assert that this path sends only login, keepalive and logout,
never `require-video-send` or wake-up. The only commands it can carry are an
explicit siren on/off or saved-position move (`set-ptz|alias|G|id|`), sent when
a user presses those buttons without an open live view, as the app's always-on
session does.

## Why an FCM registration token alone is insufficient

The full-account notification path has these concrete parts:

1. `MyQMessagingService.java:onNewToken` passes a Firebase token to
   `RegisterNotificationWorker`; its base `FCMTokenRegistrationWorker` can obtain
   one from `FirebaseMessaging.getToken()`.
2. `RegisterNotificationWorker.java:q` builds `l91/PushNotificationsConfiguration`.
   `r91/a.java` maps it to `q91/CreatePhoneNotificationDto` with
   `push_notification_token`, `product`, `model`, `os_version`, `platform`, and
   `app_version`. `p91/a.java` registers using **PUT**
   `https://notifications.myq-cloud.com/api/v6.0/notifications/phones/{phoneId}`;
   DELETE on the same path unregisters it (`lj2/p` is PUT, `lj2/b` is DELETE).
   Host/version are established by `m91/a.java` and `o91/c.java`.
3. Delivery enters `FirebaseMessagingService` as an Android Intent and then
   `MyQMessagingService.onMessageReceived`. The app receives data keys including
   `type`, `subType`, `notificationType`, `accountId`, `serial_number`, and `eventId`.
   It dispatches device/diagnostic/invite/video/call notifications and selectively
   invalidates camera cache through `com/chamberlain/myq/main/z4.java:y0`.
   These notification payloads are not a general authoritative device-state DTO.

The missing execution piece is an authenticated **receiver** for this project's
Firebase installation. The APK's Firebase client delegates registration/delivery
to Google Play services via Android IPC (`wm1/f.java`,
`com.google.android.c2dm.intent.REGISTER`, package `com.google.android.gms`).
The Play-services process owning the persistent Google delivery channel is not
part of this APK export. The registration token identifies that installation's
delivery destination; it is neither a polling credential nor a URL to receive
messages. This workspace has no genuine installation token plus working
receiver/helper that can forward those received messages to Home Assistant.
The existing browser helper supplies App Check/OAuth data, not that receiver.

Consequently, there is no registration-only toggle pretending to enable push,
no reassignment of the user's phone registration, and no fabricated FCM listener.
Full-account garage/lock/invite push requires a genuine receiver/forwarder and
its authenticated handoff before the evidenced registration and event routing
can form an end-to-end integration. Camera idle status is implemented through the
independent, fully traced Tend channel described above.

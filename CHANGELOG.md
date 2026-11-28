# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- Setup and **Reconnect MyQ** open on the email, password and debug token form
  again, instead of a choice of sign-in method. Browser sign-in is a **Sign in
  with a browser** switch on that form: turn it on and enter only the debug
  token (or leave it blank to use an installed `myq_app_check.json`). Email and
  password aren't needed, and anything typed in them is not sent.

## [0.20] - 2026-11-28

### Added

- Setup and **Reconnect MyQ** open with a choice: **Sign in with email and
  password** or **Sign in with a browser**. Browser sign-in asks only for the
  debug token (or uses an installed `myq_app_check.json`) and checks it with
  Firebase before showing MyQ's sign-in link. Your password is entered only on
  MyQ's page.

### Changed

- When email and password sign-in continues in a browser, the browser sign-in
  screen shows a notice saying why.

## [0.19] - 2026-11-28

### Added

- Browser sign-in. When MyQ's sign-in pages show a browser check, or a screen
  the integration does not handle such as single sign-on, setup and reconnect
  continue with **Sign in with a browser** instead of stopping. Sign in on
  MyQ's page in a browser on a computer, then paste the `com.myqops://android`
  address it tries to open, or the browser console message containing it. The
  debug token already entered proves the app for the code exchange. See
  [browser sign-in](https://github.com/kedube/ha-myq/blob/main/docs/sign-in.md#browser-sign-in).

### Changed

- MyQ's sign-in pages are requested with Chrome's TLS and HTTP/2 profile,
  matching the Chrome identity they already sent, instead of Python's. This
  makes MyQ's browser check less likely to appear.

## [0.18] - 2026-11-27

### Fixed

- Sign-in now verifies the Firebase App Check debug token before sending a
  MyQ password or requesting a verification code. The proof is reused for
  the code exchange and for retries after a mistyped password, and refreshed
  if a long sign-in makes it stale.
- Firebase rate limits now retain their retry delay and show the rate-limit
  error. The cooldown survives retrying or restarting setup, rather than
  allowing another request immediately. A MyQ sign-in cooldown also survives
  restarting setup, for that account only.
- Joining an opener and its separately registered Wi-Fi gateway no longer
  fails on Home Assistant 2026.8, which stopped the integration from loading.

### Changed

- Compatibility checks now run the full test suite on current Home Assistant
  as well as the existing 2026.2.3 baseline. Current HA uses its official
  `pytest-homeassistant` fixtures in a separate environment.
- Dependency checks cover x86-64 and ARM64. Native runners also install the
  integration's requirements in the minimum and current official Home
  Assistant containers, import the platforms and camera libraries, send a
  loopback request through the native HTTP clients, and open the setup form
  without an account.

## [0.17] - 2026-11-25

### Added

- The MyQ panel and door card draw a door that MyQ reports stopped partway as
  partly open, labeled **Partly open**, instead of fully open. MyQ does not
  report how far a door is open, so it is drawn between open and closed.
- Door entities have a `door_state` attribute with MyQ's own door state, so
  automations can tell a door stopped partway (`stopped`) from one that is
  fully open. Home Assistant still shows both as open.

## [0.16] - 2026-11-25

### Fixed

- The MyQ panel's camera live view had no sound, and its speaker button could
  not unmute it. The panel started Home Assistant's player muted, and Home
  Assistant's WebRTC player leaves the sound out of a stream it starts muted.
  The panel now starts it unmuted, as the camera's own dialog does.

## [0.15] - 2026-11-25

### Fixed

- TC camera live views could still have no sound, with the player's speaker
  button unavailable. A view's stream listed an audio track only if the
  camera's sound had begun within five seconds of the session opening;
  otherwise it started without one, and go2rtc never looks for one later. The
  stream now lists its audio from the start and carries silence until the
  camera's sound arrives, so a view no longer waits for the sound to start.

### Added

- Diagnostics show how long the camera's sound took to begin, and how many
  players were told of the sound when they started.

## [0.14] - 2026-11-24

### Fixed

- Live video from TC cameras had no sound, so the player's speaker button stayed
  muted. As the MyQ app does, a live view now opens the camera's audio
  connection with its video, and the stream carries the camera's sound for
  WebRTC players: Opus as the camera sends it, or the camera's G.711 converted
  to Opus. A new view waits up to five seconds for the sound before it starts.
  Home Assistant's HLS player still has no sound from these cameras.
- The **Speaker** failed with "The camera does not name a talkback codec" on
  cameras that list no talkback codec in their details or their Tend login row.
  It now talks in the codec of the camera's own sound, as the app does.

### Changed

- The TC camera stream source is an MPEG transport stream instead of bare
  H.264. The video is still the camera's own, not re-encoded.
- The **Speaker** talks over an open live view's audio connection rather than a
  second one, as the app does.

### Added

- Diagnostics show the codec of the camera's sound, the Opus packets served and
  frames dropped, and the mode and codecs of the camera's audio connection.

## [0.13] - 2026-11-23

### Changed

- A Wi-Fi garage door opener and its built-in gateway have one
  **Connectivity**, **Device type** and **Last state change** entity instead of
  two of each. Connectivity is on only while both are online, and its
  `opener_online` and `gateway_online` attributes show which one is offline.
  The gateway's copies are removed, so automations that used them need to use
  the opener's.
- A camera's **Firmware** entity shows Home Assistant's update icon instead of
  the MyQ logo.

### Fixed

- Cameras built into openers report their power state as a short code (`FP`
  for full power). It was read as unknown, so every live view sent a needless
  wake-up request first, and 0.10 waited for a full-power state that never
  came. The short codes now read as the full names.
- TC cameras that advertise talkback but list no codecs in their details (as
  cameras built into openers do) now get a **Speaker**. It talks in the codec
  the camera's Tend login row lists.

### Added

- Diagnostics show the talkback codecs a TC camera's Tend login row lists.

## [0.12] - 2026-11-23

### Added

- Diagnostics show a sleep mode the integration does not recognise as the
  camera sent it, each camera's speaker capabilities, and whether it has a
  Speaker.

## [0.11] - 2026-11-23

### Fixed

- Fix live video failing with "MyQ camera did not wake up in time". Live video
  no longer waits up to 30 seconds for the camera to report full power, which
  cameras built into openers did not do. It wakes a camera that may be asleep
  the way the MyQ app does for the account: through Tend's video service
  (`wakeup=1|` on the video request) when MyQ enables `enable_vmw_wakeup`,
  otherwise with the wake-up request, and gives the camera 30 seconds to
  answer. This applies to TC cameras and to WebRTC cameras.
- A camera Tend could not wake now says why ("did not wake up in time", "cannot
  be woken right now", "already being woken") instead of a generic refusal.

### Added

- TC camera live video falls back as the MyQ app does when UDP to the camera
  does not get through: UDP through Tend's relay (modes 10/11, for cameras that
  list it), then Tend's TLS video channel (mode 0). A camera without an AES key
  is asked for mode 8, as in the app. Modes MyQ lists as failing for a camera
  are skipped when the account has `enable_connection_mode_override`.
- A **Speaker** media player for TC cameras that advertise talkback. Text to
  speech, media files and audio addresses play through the camera's speaker,
  encoded as Opus or G.711 as the camera allows, over the live view's session
  or a session of its own. On cameras that list stored sounds, the speaker
  also plays those.
- The integration reads the account's MyQ feature flags (`my/features`) to
  decide how live video wakes cameras and which modes to skip.
- Diagnostics show each camera's power state (online, sleep mode and interval,
  battery, hardware model), what the last live-video wake-up did, the account's
  live-video feature flags, and the video modes a TC camera tried and used, so
  a camera that does not stream can be diagnosed.

## [0.10] - 2026-11-23

### Fixed

- Fix every connection to MyQ's video service failing, which left live video,
  the background camera status connection and camera commands (siren, saved
  positions) not working. The service redirects each login to a broker that
  presents the main server's certificate; the broker is now verified against
  that name instead of its own, which never matched.
- Fix TC camera live video (the cameras built into garage door openers, among
  others). Release 0.09 decoded and re-encoded the video inside Home Assistant,
  which is slow on a Raspberry Pi and lowers picture quality. TC cameras are now
  a Home Assistant stream source: Home Assistant's own players (WebRTC through
  its built-in go2rtc, or HLS) play the camera's H.264 unchanged. This has local
  tests only; playback on a physical camera is not yet confirmed.
- Make the TC video exchange as tolerant as clients known to work with these
  cameras: other messages before the login answer, a camera listed under its
  opener, any acceptance message, undecodable service messages, a first
  connection attempt that times out (retried once), video arriving from another
  port or through the service's relay, and partly malformed video packets no
  longer end a session.
- Fix a door, lock, light or settings command being sent to MyQ twice with no
  error shown. The network library re-sends any request when a connection it is
  reusing fails before an answer arrives; commands and session renewals now use
  a connection of their own, and such a failure is reported as "outcome
  unknown" instead.
- Fix a timed siren that could be left sounding: when the request to start it
  had an unknown outcome, when a second press failed, or when "stop" was
  pressed while the camera's details were unavailable. The stop is now
  scheduled before the siren is asked to sound and is cancelled only once a
  stop has been sent.
- Fix live video ending whenever one MyQ poll, or one re-read of that camera's
  details, failed. A camera's last details are kept for up to 30 minutes when a
  re-read fails.
- Fix TC camera video starts that Home Assistant gave up on after 30 seconds
  beginning again from the login on every retry. The start now continues and
  the retry joins it. Turning the camera off, unloading and stopping Home
  Assistant end a start immediately instead of waiting up to 90 seconds, and
  stopping Home Assistant now closes camera sessions.
- Fix the Home Assistant log filling with "does not generate unique IDs" errors,
  one per poll, after an entity ID was renamed, and entities that are disabled
  being built and refused again on every poll.
- Fix every device-targeted `myq.*` action failing with "device not found" for
  a garage door once a helper entity (template, threshold and similar) was
  attached to its device.
- Fix a camera setting showing its old value for up to 15 minutes after a
  successful change when a poll ran during the change, and a camera's pushed
  status update being overwritten by a poll already in progress.
- Fix MyQ WebRTC live views using memory without limit while a viewer's browser
  could not complete its connection, and a camera reporting "busy" until
  restart after one viewer's connection failed or a start hit an unexpected
  error.
- Fix a session renewal that MyQ had already answered being lost when the
  integration was unloaded at that moment, which then required signing in again.
- Fix a reconnect delay for the background camera connection that only ever
  grew, reaching 15 minutes after four ordinary disconnects.
- Fix an unreadable saved session failing setup with an error instead of asking
  for a new sign-in.
- Smaller fixes: a signed MyQ WebRTC address written without a path was
  rejected; a long recording playlist was silently cut short; a recording
  thumbnail was served without checking that the event belongs to the selected
  camera; an accessory could be renamed to an existing name by adding a space;
  a rate-limited command was watched as if it might have run; removing a device
  while the integration was not loaded raised an error; and malformed values
  from MyQ in several places raised a Python error instead of a MyQ one.

### Changed

- A camera whose serial starts with `TC` always uses the stream-source route,
  whatever it reports about MyQ's WebRTC capability. Other cameras with that
  capability keep the integration's WebRTC relay.
- TC live video is video only and has no in-view pan/tilt or siren buttons. Its
  camera session is shared by everything reading the stream and closes about 15
  seconds after the last reader stops. Home Assistant stream features such as
  `camera.record` now apply to TC cameras.
- Diagnostics report each camera's live video route, the TC stream source's
  counters and last error, and whether the camera status connection is up, so a
  download shows where live video stopped. No addresses or identifiers are
  included.
- A failed session renewal is not sent again for 30 seconds, and never again
  with a session MyQ has ended. A request answered "unauthorized" within five
  minutes of a renewal fails by itself instead of renewing again, so one
  endpoint that always refuses cannot rotate the session on every poll.
- The actions that only read (`myq.get_*`), pan/tilt, saved positions and the
  siren no longer discard cached roles and camera details and force a full poll.
- Door clip links for the Activity entity are read again only when the door's
  history changes, or while a recent event may still be waiting for its clip.
- TC video is accepted only on a UDP port the camera has confirmed. A TC
  session that provides no playable keyframe within 25 seconds ends with that
  reason in diagnostics, which now also count records that were not H.264.
- Siren and saved-position commands for a TC camera use its open video session
  when there is one, instead of a second login beside it.
- Home Assistant 2026.11's entry-scoped device lookups are used throughout,
  which removes its deprecation warnings for this integration.
- The documentation is rewritten and reduced to the README, a
  [sign-in guide](https://github.com/kedube/ha-myq/blob/main/docs/sign-in.md),
  a [user guide](https://github.com/kedube/ha-myq/blob/main/docs/user-guide.md),
  an [API reference](https://github.com/kedube/ha-myq/blob/main/docs/api.md) and
  [development notes](https://github.com/kedube/ha-myq/blob/main/docs/development.md).
  The research notes and generated route catalogs are no longer published.

## [0.09] - 2026-11-22

## [0.08] - 2026-11-22

### Fixed

- Preserve the device list's top-level `parent_device_id` so Wi-Fi opener/gateway
  pairs merge when MyQ supplies that parent relationship instead of `gateway_id`.
  Diagnostics now distinguish unmatched pairs from blocked registry merges without
  exposing device identifiers.
- Accept JSON-quoted debug tokens, copied `debug_token` lines, and registration
  JSON objects in the sign-in form. Only the extracted credential is sent to
  Firebase; malformed pasted JSON is rejected before sign-in.

## [0.07] - 2026-11-22

### Changed

- Combine an explicitly linked Wi-Fi garage opener and its built-in Wi-Fi gateway
  into one Home Assistant device. Existing entities and their IDs are preserved;
  gateway diagnostics move under the opener. Shared or unrelated hubs stay separate.
  Device-target actions and sidebar status use the opener within the combined device.

### Added

- A masked **Debug token** field follows email and password in setup and
  reauthentication. Entering a registered token uses bundled public Firebase app
  metadata without a separate JSON installation. The token is not saved;
  existing registration files remain available when the field is blank.
- A private install ZIP can include both the integration and an existing App Check
  registration, installing them together into Home Assistant's configuration
  directory. Public release archives continue to exclude registration credentials.

## [0.06] - 2026-11-22

### Fixed

- Check the private App Check registration before sending credentials or asking
  for MFA. Missing, unreadable and invalid registration files now have distinct
  messages with installation guidance, separate from Firebase service failures.

## [0.05] - 2026-11-22

### Fixed

- Fix "Invalid handler specified" when opening MyQ setup with curl_cffi 0.17
  already installed. Pass browser aliases to its public session API instead of
  importing the internal `normalize_browser_type` helper removed in that version.
- Fix the rate-limit cooldown escalating too fast and not clearing. A single 429
  without a `Retry-After` header was counted twice (once by the transport, once
  by the client), doubling the back-off step per response, and a native MyQ host's
  streak never reset after a later success, so the cooldown stayed elevated. Each
  429 now advances the streak once, and any non-429 response clears it.

## [0.04] - 2026-11-22

### Added

- Katherine Dubé's sidebar panel, dashboard door card and customizable animated
  door artwork, reused with permission. Includes appearance and layout settings,
  camera views, activity, translations and open-door warnings.
- Panel inventory uses this integration's account-scoped entity IDs, associated
  camera devices and Home Assistant read permissions. Sidebar options apply
  without reloading the MyQ connection.

### Changed

- Email/password sign-in now acquires Firebase App Check proof automatically
  using a private registered debug-provider configuration. Proof is cached with
  expiry checks and attached only to the one-time code exchange. Firebase
  acquisition was verified; a complete MyQ sign-in is still unverified.
- Hosted form requests use the unified authorization flow and mobile Chrome
  identity from the working-reference behavior. Credential/OTP field variants,
  email MFA selection, checked controls and post-MFA consent are supported,
  including resuming authorization after consent returns HTTP 200.
- Native OAuth and MyQ API requests use a measured Android TLS/HTTP2
  profile through curl_cffi, now a runtime dependency. Cookie isolation and
  native connection-pool cleanup are covered by local tests.
- The selected model is corrected from the emulator's display name to its actual
  Build.MODEL. The private `myq_app_check.json` registration is excluded from
  source control and release archives; install it in HA's configuration directory.

## [0.03] - 2026-11-21

### Changed

- Connect MyQ now opens directly to email and password. Setup and reauthentication
  no longer offer browser handoff, refresh-token import, or manual app-verification
  token fields. MFA and automatic session renewal remain available. This UI change
  does not resolve MyQ's browser challenge or app-verification requirement.

### Fixed

- Native authentication and MyQ API requests now use an Android device-format
  User-Agent, the app's gzip/header defaults, and no browser cookies or client
  hints. OAuth forms use the Android client's encoding. Browser navigation keeps
  its separate identity. This does not supply App Check proof or establish a
  successful login; Android TLS and HTTP/2 equivalence remain unverified.

### Removed

- The "One-time MyQ sign-in link" setup option. MyQ's link page,
  `https://www.myliftmaster.com/ids/auth/onetime-login`, now says web login is
  no longer supported and to use the mobile app instead.

## [0.02] - 2026-11-21

### Fixed

- Adding the integration failed with "Config flow could not be loaded: 500
  Internal Server Error" on Home Assistant 2026.7 and later. Those versions pin
  a newer PyAV than aiortc 1.14.0 allows, so the requirement could not be
  installed. The integration now requires aiortc 1.16.0.

## [0.01] - 2026-11-21

First public release. Nothing in it has been tested against a
real MyQ account yet; see the [README](https://github.com/kedube/ha-myq/blob/main/README.md) for what that means.

### Added

- Garage doors and gates, lamps, locks, linked thermostats and cameras, with the
  MyQ app's safety checks, health sensors, opener and lock settings, recordings,
  live video and two-way audio. See [devices](https://github.com/kedube/ha-myq/blob/main/docs/user-guide.md#devices-and-entities) and
  [cameras](https://github.com/kedube/ha-myq/blob/main/docs/user-guide.md#cameras).
- Sign-in through a browser helper with an App Check token, a one-time MyQ
  sign-in link, or an existing refresh token. Deleting the integration ends its
  MyQ session.
- Account management actions for accessories, lock PINs, guests, camera
  schedules, alerts and device names. See [actions](https://github.com/kedube/ha-myq/blob/main/docs/user-guide.md#actions).
- Translations into German, Spanish, French, Italian, Japanese, Dutch, Polish,
  Brazilian Portuguese and Simplified Chinese, including entity names, select
  options and error messages.
- An integration icon (a neutral garage drawing, not MyQ's logo).

### Changed

These follow a review of the integration against the MyQ Android app's code.

- Settings and actions are offered by account role, as the app's screens do, and
  the account you created counts as yours even when MyQ's role data cannot be
  read. The app never uses the per-account permission lists, so the integration
  no longer does either.
- Thermostat changes round targets as the app does, instead of refusing values
  Home Assistant converted from another unit. They keep the heating and cooling
  gap by moving the other target and carry the fan inside the mode change. The
  thermostat is re-read two seconds after a change. Off is always offered.
- Guests can be given only the device types the app offers them. Accessory
  names follow the app's 24-character, no-duplicates rule. Enabled camera
  schedules may not overlap.
- Fewer requests: camera details, opener settings, alert rules, roles and the
  accounts list are re-read on longer intervals, as the app reads them only when
  a screen opens. The poll interval is at least 60 seconds. Repeated rate limits
  back off up to an hour, and a cooldown survives setup retries.

### Known limitations

- Removing a guest, or taking a video keypad away from one, does not delete the
  guest's keypad PINs; the app does, through a service this integration cannot
  reach. Remove them in the MyQ app first.
- The integration's domain is `myq`, the same as Home Assistant's own MyQ
  integration, which was removed in 2023, and other MyQ custom integrations. It
  replaces any of them installed in `custom_components/myq`. Entries the removed
  integration left behind fail to set up with a message to delete them.
- Signing in has not worked yet; see the [sign-in guide](https://github.com/kedube/ha-myq/blob/main/docs/sign-in.md).

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

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
  live video and two-way audio. See [devices](https://github.com/kedube/ha-myq/blob/main/docs/devices.md) and
  [cameras](https://github.com/kedube/ha-myq/blob/main/docs/cameras.md).
- Sign-in through a browser helper with an App Check token, a one-time MyQ
  sign-in link, or an existing refresh token. Deleting the integration ends its
  MyQ session.
- Account management actions for accessories, lock PINs, guests, camera
  schedules, alerts and device names. See [actions](https://github.com/kedube/ha-myq/blob/main/docs/integration-actions.md).
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
- Signing in has not worked yet; see the [sign-in guide](https://github.com/kedube/ha-myq/blob/main/docs/sign-in.md#what-is-known).

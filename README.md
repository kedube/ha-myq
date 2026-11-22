# MyQ Integration for Home Assistant (experimental)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/v/release/kedube/ha-myq)](https://github.com/kedube/ha-myq/releases)
[![CI](https://github.com/kedube/ha-myq/actions/workflows/ci.yml/badge.svg)](https://github.com/kedube/ha-myq/actions/workflows/ci.yml)
[![Validate](https://github.com/kedube/ha-myq/actions/workflows/validate.yml/badge.svg)](https://github.com/kedube/ha-myq/actions/workflows/validate.yml)
![Home Assistant 2026.2+](https://img.shields.io/badge/Home%20Assistant-2026.2%2B-41BDF5.svg)
[![License](https://img.shields.io/github/license/kedube/ha-myq)](LICENSE)

A Home Assistant integration for Chamberlain and LiftMaster **MyQ** garage door
openers, gates, lamps, cameras, locks and linked thermostats. It is an
independent client implementation written from the MyQ Android app protocol
(version 5.324.0.1132442) and MyQ's hosted sign-in pages. It does not use any
other MyQ client library, and the repository contains no decompiled app code.
The sidebar panel, dashboard door card and door artwork are Katherine Dubé's
UI, reused with her permission.

> **Unofficial.** Not affiliated with or endorsed by The Chamberlain Group, which
> owns the MyQ, Chamberlain and LiftMaster brands. MyQ's API is private and can
> change without notice. See the [API reference](docs/api/README.md).

[Features](#features) · [Requirements](#requirements) · [Install](#install) ·
[Set up](#set-up) · [How it behaves](#how-it-behaves) ·
[Privacy and security](#privacy-and-security) · [Documentation](#documentation) ·
[Development](docs/development.md)

> [!WARNING]
> **Untested against MyQ.** No one has signed in to MyQ with this integration
> yet, so it is not known whether any sign-in method works. The
> [sign-in guide](docs/sign-in.md#what-is-known) explains what
> is known.
>
> Everything after sign-in is written from the MyQ app's protocol and tested
> only against simulated MyQ responses. None of it has run against a real
> account, door, lock or camera.

## Features

| Area | What you get |
| --- | --- |
| Garage panel | Optional sidebar with animated, customizable door drawings, camera views, activity and open-door warnings. The same doors can appear as dashboard cards. |
| Garage doors and gates | A `cover` entity with open/close and reported state. The app's safety checks apply: monitor-only, PIN lockout, work-light error, virtual-opener sensor fault and the unattended open/close flags. |
| Opener settings | Light timer, time-to-close, motion-activated light and **Disable remotes** (lock mode), for the account owner, as in the app. |
| Lamps | A `light` entity with on/off. An opener's work light is not treated as a lamp. |
| Locks | A `lock` entity for Wi-Fi, partner and gateway-connected locks. Also: PIN lockout state and a **Clear PIN lockout** button, door contact, calibration, auto-lock settings and firmware. |
| Thermostats | A `climate` entity with the device's modes, fan modes and setpoint limits. Presets and schedules are readable through an action. |
| Cameras | Snapshots and live video through MyQ's WebRTC route or the older TC-camera Tend route, relayed to a WebRTC browser. Up to four viewers share one MyQ session. TC live video has no audio or talkback. Pan/tilt/zoom, siren, saved positions, two-way audio and settings are offered where supported. |
| Recordings | Recorded clips in **Media → MyQ recordings**, with thumbnails, detection labels and a **Door events** folder. |
| Device health | Connectivity, battery, charging, Wi-Fi strength, fault codes, door cycles, last state change, backup battery and firmware updates, when MyQ reports them. |
| Activity and alerts | Optional **Activity** event entities per device, switches for each notification rule, and actions to read history and snooze alerts. |
| Account management | Administrator actions for accessories, lock PINs, guest access, camera schedules and device names. |
| Other devices | Built-in Wi-Fi gateways share their opener's device and contribute diagnostics. Standalone gateways, hubs and unrecognised devices appear with their type and diagnostics, without controls. |

Details: [devices and entities](docs/devices.md), [cameras](docs/cameras.md),
[actions](docs/integration-actions.md).

**Not supported:** garage door stop or position control, Bluetooth-only lock
operations, device pairing and setup, firmware installation, and push
notifications for doors and locks (state is polled). Live video needs MyQ's
WebRTC capability or a supported TC camera. Other cameras retain snapshots and
recordings. The new TC live-video implementation is tested locally; it has not
yet been verified on a physical camera.

## Requirements

- Home Assistant 2026.2 or later. Tested with 2026.2.3 and Python 3.13.
- Your MyQ account email and password, plus access to its verification codes.
- A registered Firebase debug token for app verification, entered during setup
  or supplied by an existing registration file. Sign-in still depends on MyQ
  accepting the application's verification; see [Set up](#set-up).
- For live video: a WebRTC-capable browser and access to MyQ's video service
  (Tend, TCP port 5104). The MyQ WebRTC route also needs AWS WebRTC access;
  TC cameras need UDP access to Tend's relay and the camera on your network.
  The `aiortc` dependency is installed automatically. The macOS companion app's
  WebRTC limitation is not addressed by the TC transport addition.

## Install

This is a custom repository: it is not in the default catalogue, so searching for it by name
finds nothing until you add the repository by link. After that it installs and updates like
any other.

It installs as `custom_components/myq`, so it replaces any other MyQ custom integration in
that folder; remove that one first. Entries left over from Home Assistant's own MyQ
integration, removed in 2023, cannot be used: delete them, then add MyQ again.

**Via the Marketplace (Home Assistant 2026.11 and later)**

From 2026.11, HACS is built into Home Assistant as the Marketplace. An install made through
HACS carries over and keeps getting updates, so there is nothing to redo. To add it fresh:

1. Open **Settings → Marketplace** and choose **Add from link**
2. Enter `kedube/ha-myq` (or the full GitHub URL) and choose **Add**
3. Install **MyQ**, and restart Home Assistant if a repair asks you to

**Via HACS (Home Assistant 2026.10 and earlier)**

[![Open this repository in HACS on your Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kedube&repository=ha-myq&category=integration)

That button adds this repository to HACS on your own instance and offers the download. It
needs [My Home Assistant](https://my.home-assistant.io/) set up in your browser. Otherwise:

1. Open **HACS**, open the ⋮ menu and choose **Custom repositories**
2. Add `kedube/ha-myq` (or the full GitHub URL) with category **Integration**
3. Search for **MyQ** and install it
4. Restart Home Assistant

Both install from **releases**, not from the latest commit: each release attaches the
`myq.zip` archive they download.

**Manually**

For the private install ZIP (`myq-install-private.zip`), extract it into Home
Assistant's configuration directory (usually `/config/`) and restart. It includes
both the integration folder and the app-verification registration, ready for the
email/password setup. Keep this ZIP private.

Copy the `custom_components/myq/` folder from this repository into the
`custom_components/` folder of your Home Assistant configuration directory, so that
`config/custom_components/myq/manifest.json` exists, and restart. Alternatively,
extract a release's `myq.zip` into `config/custom_components/myq/`.

## Set up

Go to **Settings → Devices & services → Add integration → MyQ**. **Connect MyQ**
opens directly to email, password, and a masked **Debug token** field. Enter your
credentials and registered debug token, then complete MyQ's verification prompt
if one appears. Your password and debug token are not saved.

Email/password is the only setup and reconnect method. End-to-end sign-in is
still unverified. App Check acquisition is implemented and verified with a
registered debug credential. The public release includes the public app
configuration; entering the debug token needs no separate JSON file. If you
already installed `myq_app_check.json` in Home Assistant's configuration directory,
you can leave the token field blank to use it. The
[sign-in guide](docs/sign-in.md) explains token entry, reauthentication, and the
remaining validation.

**Options:** the poll interval (60–900 seconds, default 60), whether cameras
are discovered (on by default), and the sidebar panel's visibility, title, icon
and administrator-only access.

### Garage panel and door card

Open MyQ's **Configure** options under **Settings → Devices & services**, turn on
**Show the garage panel in the sidebar**, and save. Doors and cameras are added
automatically. Administrators can customize the door drawings (style, color,
windows, trim and hardware), arrange the doors, set the open-door warning time,
and enable command confirmation. These preferences are shared with other viewers
and survive restarts. Sidebar changes apply without reconnecting to MyQ.

The **MyQ garage door** dashboard card is registered automatically, including when the
sidebar is hidden. Select it in the card picker or add it in YAML:

```yaml
type: custom:myq-door-card
entity: cover.your_garage
```

Use `compact: true` for a smaller card. Cameras associated with an opener appear
with that door; other cameras have their own panel cards. Live video uses the
existing Home Assistant camera entity and its supported streaming capabilities.
The separate [live camera card](docs/cameras.md) remains available.

## How it behaves

This is how the integration is built to behave, following the MyQ app. None of
it has been confirmed against MyQ.

- **Polling:** device state is read on the poll interval. Things the app reads
  only when you open a screen are read less often: camera details every 15
  minutes (or when the camera reports a change), opener settings, alert rules
  and account roles every 30 minutes, and the accounts list every 6 hours; any
  change made from Home Assistant re-reads them sooner. After a door, lock or lamp
  command, or while a poll shows a door or lock moving, that device's account is
  re-read every 5 seconds until it reaches its target state. The app's time
  limits apply: 30 seconds for locks, 60 for openers and lamps, 90 for virtual
  openers and 120 for gates and commercial doors.
- **Commands** are never queued. A command answered with an HTTP 401 that the
  app would renew is sent once more after renewing the session, because MyQ did
  not act on it. Any other failure is reported, not repeated: a timeout can leave
  the outcome unknown, and the next reported state is what counts.
- **Roles:** as in the app, settings and actions are offered by account role: for
  example opener and camera settings to the owner, alert snooze to owners and
  co-owners, history to everyone but guests. The account you created is yours,
  even if MyQ's role data cannot be read.
- **Rate limits:** when MyQ answers "too many requests", nothing is sent to that
  MyQ service until its requested wait is over; repeated limits without a stated
  wait back off from one minute up to an hour.
- **Sessions** renew on the app's schedule. Home Assistant asks you to sign in
  again only when MyQ ends the session, as the app does; other renewal errors are
  retried on the next poll.
- **Removal:** deleting the integration cancels its MyQ session. Other sign-ins,
  such as the phone app, are not affected.
- **Partial failures:** a camera or settings failure does not hide working door
  or lock entities.

## Privacy and security

- OAuth tokens are stored in Home Assistant's config-entry storage. They are
  sensitive and are included in Home Assistant backups.
- Passwords and verification codes are never stored.
- Diagnostics exclude tokens, names, serial numbers, account IDs and camera
  payloads.
- The browser never receives a MyQ token or cloud media address; recordings and
  live video are relayed through Home Assistant.

## Documentation

| Section | Contents |
| --- | --- |
| [Using the integration](docs/README.md#using-the-integration) | Sign-in, devices and entities, cameras, actions |
| [MyQ API reference](docs/api/README.md) | What is known about MyQ's services: authentication, devices, cameras and video, recordings, management, routes |
| [Research notes](docs/research/README.md) | How that knowledge was gathered: investigation history, evidence ledgers, test records and research tools |
| [Development](docs/development.md) | Local setup, tests, packaging and the project layout |
| [Changelog](CHANGELOG.md) | What changed in each release |

## License

AGPL-3.0. See [LICENSE](LICENSE).

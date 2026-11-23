# MyQ Integration for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/v/release/kedube/ha-myq)](https://github.com/kedube/ha-myq/releases)
[![CI](https://github.com/kedube/ha-myq/actions/workflows/ci.yml/badge.svg)](https://github.com/kedube/ha-myq/actions/workflows/ci.yml)
[![Validate](https://github.com/kedube/ha-myq/actions/workflows/validate.yml/badge.svg)](https://github.com/kedube/ha-myq/actions/workflows/validate.yml)
![Home Assistant 2026.2+](https://img.shields.io/badge/Home%20Assistant-2026.2%2B-41BDF5.svg)
[![License](https://img.shields.io/github/license/kedube/ha-myq)](LICENSE)

A Home Assistant integration for Chamberlain and LiftMaster **MyQ** garage door
openers, gates, lamps, cameras, locks and linked thermostats. It is an
independent client written from the MyQ Android app's protocol and MyQ's hosted
sign-in pages; it uses no other MyQ client library and contains no decompiled
app code. The sidebar panel, dashboard door card and door artwork are my own UI and used with permission for this integration.

> **Unofficial.** Not affiliated with or endorsed by The Chamberlain Group, which
> owns the MyQ, Chamberlain and LiftMaster brands. MyQ's API is private and can
> change without notice.

[Features](#features) · [Requirements](#requirements) · [Install](#install) ·
[Set up](#set-up) · [Documentation](#documentation)

## Features

| Area | What you get |
| --- | --- |
| Garage panel | Optional sidebar with animated, customizable door drawings, camera views, activity and open-door warnings. The same doors can appear as dashboard cards. |
| Garage doors and gates | A `cover` entity with open/close and reported state, with the app's safety checks. |
| Opener settings | Light timer, time to close, motion-activated light and **Disable remotes**, for the account owner. |
| Lamps | A `light` entity with on/off. |
| Locks | A `lock` entity for Wi-Fi, partner and gateway-connected locks, with PIN lockout, door contact, auto-lock settings and firmware. |
| Thermostats | A `climate` entity with the thermostat's modes, fan modes and setpoint limits. |
| Cameras | Snapshots and live video. TC cameras (such as those built into openers) play through Home Assistant's own WebRTC or HLS without re-encoding. Cameras with MyQ's WebRTC capability are relayed to the browser, with two-way audio. A speaker media player sends text to speech and audio files to TC cameras with talkback. Pan/tilt/zoom, siren, saved positions and camera settings where the camera supports them. |
| Recordings | Recorded clips in **Media → MyQ recordings**, with thumbnails, detection labels and a **Door events** folder. |
| Device health | Connectivity, battery, Wi-Fi strength, fault codes, door cycles, backup battery and firmware updates, when MyQ reports them. |
| Activity and alerts | Optional **Activity** event entities, a switch for each notification rule, and actions to read history and snooze alerts. |
| Account management | Actions for accessories, lock PINs, guest access, camera schedules and device names. |

**Not supported:** stopping or positioning a door, Bluetooth-only lock
operations, device pairing and setup, firmware installation, and push updates
for doors and locks (their state is polled).

## Requirements

- Home Assistant 2026.2 or later.
- Your MyQ email and password, and access to its verification codes.
- A registered Firebase App Check debug token for this integration's app
  registration. See [the debug token](docs/sign-in.md#before-you-start-the-debug-token)
  before you start.
- For live video: outbound TCP port 5104 to MyQ's video service. TC cameras
  also use UDP to MyQ's video relay and the camera; where UDP is blocked they
  fall back to MyQ's TLS video channel. Cameras on MyQ's WebRTC route need a
  WebRTC-capable browser.

## Install

The integration installs as `custom_components/myq` and replaces any other MyQ
custom integration in that folder; remove that one first. Entries left over
from Home Assistant's own MyQ integration, removed in 2023, can't be used:
delete them, then add MyQ again.

**Marketplace (Home Assistant 2026.11 and later).** HACS is built in as the
[Marketplace](https://www.home-assistant.io/integrations/marketplace) from
2026.11, and installs made through HACS carry over.

1. Open **Settings → Marketplace** and choose **Add from link**.
2. Enter `kedube/ha-myq` and choose **Add**.
3. Install **MyQ**, and restart Home Assistant if a repair asks you to.

**HACS (Home Assistant 2026.10 and earlier).**

[![Open this repository in HACS on your Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kedube&repository=ha-myq&category=integration)

Or add it by hand:

1. Open **HACS**, then ⋮ → **Custom repositories**.
2. Add `kedube/ha-myq` with category **Integration**.
3. Search for **MyQ**, install it, and restart Home Assistant.

Both install the latest **release**, not the latest commit.

**Manually.** Extract a release's `myq.zip` into
`config/custom_components/myq/` (or copy this repository's
`custom_components/myq/` folder there), so that
`config/custom_components/myq/manifest.json` exists, and restart. A private
install ZIP is extracted into the configuration directory instead; see
[the registration file](docs/sign-in.md#the-registration-file).

## Set up

Go to **Settings → Devices & services → Add integration → MyQ**, enter your MyQ
email, password and debug token, and enter the verification code if MyQ asks
for one. The [sign-in guide](docs/sign-in.md) covers the details.

**Options** (**Configure** on the integration): the poll interval (60–900
seconds, default 60), whether cameras are discovered (on by default), and the
sidebar panel's visibility, title, icon and administrator-only access.

### Garage panel and door card

Turn on **Show the garage panel in the sidebar** in the options. Doors and
cameras are added automatically. Administrators can customize the door drawings
(style, colour, windows, trim and hardware), arrange the doors, set the
open-door warning time and require command confirmation. These preferences are
shared with every viewer.

The **MyQ garage door** dashboard card is available whether or not the sidebar
is shown. Pick it in the card picker, or add it in YAML:

```yaml
type: custom:myq-door-card
entity: cover.your_garage
```

Add `compact: true` for a smaller card. Cameras linked to an opener appear with
that door. The separate [live view card](docs/user-guide.md#two-way-audio) adds
talkback for cameras that support it.

## Documentation

| Document | Contents |
| --- | --- |
| [Sign-in](docs/sign-in.md) | The debug token, signing in, staying signed in, error messages |
| [User guide](docs/user-guide.md) | Devices and entities, updates, cameras and live video, recordings, actions, diagnostics |
| [API reference](docs/api.md) | MyQ's cloud API: authentication, devices, commands, management, cameras, live video and recordings |
| [Development](docs/development.md) | Local setup, tests, packaging, releases, code layout and research tools |
| [Changelog](CHANGELOG.md) | Changes in each release |

## License

AGPL-3.0. See [LICENSE](LICENSE).

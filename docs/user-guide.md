# Using the integration

- [Devices and entities](#devices-and-entities)
- [How updates work](#how-updates-work)
- [Cameras](#cameras)
- [Recordings](#recordings)
- [Actions](#actions)
- [Diagnostics and privacy](#diagnostics-and-privacy)

## Devices and entities

The integration finds the devices in every MyQ account your sign-in can see.
An entity is created only for what MyQ reports for a device: a missing field, or
a capability the device does not advertise, gets no entity. Devices added in the
app appear on a later poll. A device that MyQ stops returning for three polls in
a row is removed from Home Assistant.

Seeing a device does not mean you can control it. As in the app, each setting
and action is offered to the account roles the app offers it to (for example,
opener and camera settings to the account owner only), and every change checks
your current role first.

### Supported devices

| Device | Entities and controls | Not supported |
| --- | --- | --- |
| **Garage door openers** | Cover with open/close and door state. Owner settings: **Opener light timer**, **Time to close**, **Motion activated opener light** and **Disable remotes**. | Stop, position, switching the work light directly |
| **Virtual (learned) doors** | Cover and door-sensor status. Monitor-only unless changed in the app. | Control while monitor-only |
| **Gates and commercial doors** | Cover with open/close. | Stop, position, pedestrian or hold-open modes |
| **Lamps** | Light with on/off. | Brightness or colour |
| **Cameras** | Camera entity, settings, controls and recordings. See [cameras](#cameras). | Live video on cameras that are neither TC cameras nor on MyQ's WebRTC route |
| **Wi-Fi and partner locks** | Lock with lock/unlock and state. Wi-Fi lock owners and co-owners also get **Automatic lock delay** and **Door sensor**. | Bluetooth-only operations, partner-lock settings |
| **Gateway-connected locks** | Lock with lock/unlock and state. Owners get **Auto relock** and **Auto relock delay**. Lock PINs through [actions](#actions). | Bluetooth-only operations |
| **Linked thermostats** | Climate entity with the thermostat's modes, fan modes and setpoint limits, and what it is doing. Presets and schedules can be read through an action. | Changing presets or schedules |
| **Gateways, hubs, remotes, wall controls and other devices** | A device-type sensor and any diagnostics MyQ reports. An opener's paired accessories can be managed through actions. | Controls, pairing, Wi-Fi setup |

A garage door can't be operated while it is offline, monitor-only, locked out
after wrong PIN attempts or reporting a work-light error, or (for a virtual door)
while its sensor reports a fault. Opening and closing also need MyQ's unattended
open and close permissions when the opener reports them. A thermostat can be
controlled only while MyQ reports it as managed.

### Diagnostic entities

These appear when MyQ reports the value:

- **Connectivity and power:** connectivity, offline reason, Wi-Fi signal,
  battery level, low battery, backup battery, and for cameras power source and
  charging.
- **Doors:** door state, last state change, door cycles, faults and fault
  codes, door sensor faults, and an unpaired work light.
- **Locks:** lock state, lock connection, door contact (**Door**), orientation,
  calibration problems, automatic-lock and door-sensor settings, and wrong-PIN
  lockout. A **Clear PIN lockout** button (disabled by default) is available to
  owners and co-owners while a lockout is active.
- **Firmware:** an update entity for locks, openers and cameras that report
  firmware versions. It shows that an update exists; installing is done in the
  app. A camera's firmware entity uses Home Assistant's update icon rather than
  the MyQ logo.
- **Every device:** a **Device type** sensor, which also shows your account role
  and the device's linked camera, opener or gateway.

Door cycles and battery levels keep long-term statistics.

### Activity, alerts and accessories

- **Activity** event entities (one per door, lamp and lock, disabled by default)
  fire an event for each new history entry. `event_type` is the device event,
  such as `open` or `locked`, and `activity` is the kind of entry. For an opener
  with a camera, a door event also carries `recording`, the media ID of the clip
  the door started. While any Activity entity is enabled, each full poll reads
  one history page per account.
- **Alert: …** switches turn each MyQ notification rule on or off.
- **Accessory battery** sensors for an opener's remotes and keypads are disabled
  by default. They update every five minutes once the `get_accessories` action
  has run, and after a restart while one is enabled.

### How devices are grouped

A Wi-Fi garage door opener and its built-in gateway share one Home Assistant
device, named after the opener, with the diagnostics of both. The device has
one **Connectivity**, **Device type** and **Last state change** entity, from
the opener. Connectivity is on only while both the opener and its gateway are
online; its `opener_online` and `gateway_online` attributes show which one is
offline. The gateway adds its Wi-Fi signal and any other diagnostics the
opener does not report.

Openers that were added before this was supported are combined automatically;
entity IDs and customizations are kept. The gateway's own Connectivity, Device
type and Last state change entities are removed, so automations that used them
need to use the opener's. Automations that targeted the gateway's device need
to target the combined device.

A camera built into an opener is listed under that opener. Standalone hubs and
gateways stay separate devices. A device can be deleted from Home Assistant only
after MyQ no longer returns it.

## How updates work

- **Polling.** Device state is read on the poll interval (60 seconds by
  default; 60–900 in the integration's options). Things the app reads only when
  you open a screen are read less often: camera details every 15 minutes, or
  when the camera reports a change; opener settings, alert rules and account
  roles every 30 minutes; the list of accounts every 6 hours. A change made from
  Home Assistant re-reads what it changed straight away.
- **After a command**, or while a poll shows a door or lock moving, that
  device's account is re-read every 5 seconds until the device reaches its
  target state, for up to 30 seconds (locks), 60 (openers and lamps), 90
  (virtual doors) or 120 (gates and commercial doors).
- **Commands are sent once.** A command is never queued or repeated, except one
  that MyQ refused because the session needed renewing, which is sent again
  after renewal. If a command times out, its outcome is reported as unknown; the
  next reported state is what counts.
- **Rate limits.** When MyQ answers "too many requests", nothing is sent to that
  MyQ service until its requested wait is over. Repeated limits without a stated
  wait back off from one minute to an hour.
- **Partial failures.** A failed camera, settings or management read does not
  make working door or lock entities unavailable.
- **Sessions** renew automatically. See [sign-in](sign-in.md#staying-signed-in).

## Cameras

Cameras need camera discovery switched on in the integration's options (it is
on by default). Each camera MyQ returns gets a camera entity.

### Snapshots

- **Battery cameras** reuse the camera's latest image so they are not woken for
  every snapshot. They take a new one if that image can't be loaded, has no
  capture time or is at least 24 hours old, while the battery is above 10% and
  wake on motion is on.
- **Other cameras**, including opener cameras, take a new image and fall back
  to the latest one.
- **During a live view** on MyQ's WebRTC route, a snapshot is the newest frame
  of that view.

Images are cached for 60 seconds. The `image_captured_at` attribute shows when
the image was taken, and `image_source` shows `cloud` or `live`.

To save an image, use Home Assistant's `camera.snapshot` action with a path
your configuration allows:

```yaml
action: camera.snapshot
target:
  entity_id: camera.garage_camera
data:
  filename: /config/www/garage.jpg
```

### On, off and availability

A camera that can be switched off has an on/off control. It counts as off only
when it reports itself off, and shows no image while off. A sleeping battery
camera stays available while MyQ shows it offline, because opening a live view
wakes it.

### Live video

Open the camera entity, or a dashboard card with live view. A sleeping camera
is woken as the view opens, which can take several seconds. How the video
reaches you depends on the camera.

**TC cameras.** Cameras whose serial number starts with `TC`, including those
built into garage door openers, are an ordinary Home Assistant stream source.
The integration receives the camera's H.264 video and passes it to Home
Assistant unchanged, with the camera's sound, and Home Assistant plays it with
WebRTC (through its built-in go2rtc) or HLS. The video is not re-encoded, so
the load on the Home Assistant host is small.

- The camera's sound plays through WebRTC. The player starts muted; unmute it
  with its speaker button. Home Assistant's HLS player carries no sound from
  these cameras.
- The view starts with the picture. The sound joins a moment later, once the
  camera's audio connection is open; until then the sound is silent. A camera
  that sends G.711 sound has it converted to Opus; one that sends Opus is
  passed on as it is.
- No in-view pan/tilt or siren buttons. Talk through the camera with its
  [speaker](#speaker).
- Every viewer, and every Home Assistant feature reading the stream, shares one
  camera session. It closes about 15 seconds after the last one stops.
- The video comes over UDP, straight from the camera or through MyQ's relay.
  Where UDP is blocked it falls back, as the MyQ app does, to MyQ's encrypted
  TLS video channel. Each fallback adds some seconds before the picture
  appears.
- Home Assistant's stream features work, including `camera.record` and
  **Preload camera stream**. Preloading keeps the camera streaming all the time;
  leave it off unless you want that.
- If the camera can't be reached, Home Assistant's player shows an error and may
  retry. The integration waits 15 seconds between camera sessions after a
  failure, writes the reason to the log once, and shows the last error in the
  camera's diagnostics.
- The video is served on an address only the Home Assistant host can reach.

**Cameras with MyQ's WebRTC capability.** Other cameras that advertise MyQ's
WebRTC capability are relayed to the browser over WebRTC. The integration
re-encodes the video for each viewer, which uses CPU on the Home Assistant host.
These cameras keep their audio features.

- Up to four people can watch one camera at once, sharing one MyQ session. A
  fifth viewer, or one who joins while the session is starting, is told the
  camera is busy.
- A view ends when the last viewer closes it, or after ten minutes. Reopen the
  camera to start a new one. A failed view does not reconnect by itself.
- A WebRTC-capable browser is needed. There is no RTSP or HLS address, no
  casting and no `camera.record`.

Cameras that are neither TC cameras nor on the WebRTC route have snapshots and
recordings only.

**Both routes.** Live video stops when the camera is switched off, goes offline
or is removed, and when the integration unloads or Home Assistant stops. A
failed MyQ poll while video is playing does not stop it. If live video fails,
the MyQ panel and door card say so and go back to snapshots; press **Live**
again to retry.

**Network.** Home Assistant needs outbound HTTPS and outbound TCP port 5104 to
MyQ's video service (Tend). TC cameras also need UDP to Tend's relay and,
ideally, to the camera on your network; video comes through the relay when the
camera can't be reached directly. The WebRTC route needs access to the AWS
servers MyQ supplies for each session. Viewing from outside your network uses
Home Assistant's normal WebRTC setup.

### Camera controls

Buttons appear for the controls a camera advertises:

| Control | Notes |
| --- | --- |
| Pan, tilt, zoom and stop | Need an open live view on the WebRTC route. Each press moves one step; a stop follows after one second. |
| **Go to …** saved positions | One button per position saved in the app. No live view needed. |
| Sound siren, stop siren | No live view needed. The siren is stopped after ten seconds. |
| Enable and mute microphone | Need an open live view on the WebRTC route. The microphone mutes itself after 30 seconds. |

Any account role can use these controls, as in the app. The timed stops are sent
by Home Assistant; they are not confirmations from the camera.

Camera settings appear as switches, numbers and selects when the camera
advertises them: microphone, motion detection and sensitivity, speaker and siren
volume, image orientation, night vision, status LED, doorbell chime and chime
type, PTZ auto tracking, wake on motion, power saver, and camera lights and
their brightness. A sleeping camera is woken before a setting is changed.

### Two-way audio

For cameras on the WebRTC route, the **MyQ live view** card adds talkback. It
loads automatically; add it to a dashboard and choose the camera and its two
microphone buttons in the card editor, or in YAML:

```yaml
type: custom:myq-live-card
entity: camera.garage_camera
microphone_on: button.garage_camera_enable_microphone_for_30_seconds
microphone_off: button.garage_camera_mute_microphone
```

**Start view** opens video and audio without a microphone. **Start with
microphone** asks for microphone permission and starts muted; hold **Hold to
talk** (or Space or Enter) to speak. Releasing it, leaving the tab, the
30-second limit or closing the view mutes the microphone. Browsers allow
microphone access only over HTTPS or on localhost. With several viewers, only
the first viewer can talk.

### Speaker

A TC camera that advertises talkback gets a **Speaker** media player. Anything
Home Assistant can play to a media player plays through the camera: text to
speech (`tts.speak`), files from **Media**, or an audio address. For example:

```yaml
action: tts.speak
target:
  entity_id: tts.home_assistant_cloud
data:
  media_player_entity_id: media_player.garage_camera_speaker
  message: The garage door is open.
```

- The integration downloads the audio (up to 10 MB), encodes it for the camera
  and sends at most the first two minutes. MP3, WAV, Ogg, FLAC, AAC and M4A
  files work.
- It talks over an open live view's audio connection, as the MyQ app does, so
  viewers hear the message from the camera. Without a live view it signs in to
  the camera for the duration of the message.
- It encodes for the camera the way the MyQ app does: in the codec the camera
  lists for its speaker, or, when it lists none, in the codec of the camera's
  own sound.
- Playing something new stops what is playing; **Stop** ends it early.
- It needs an account role that can control devices.

A camera that lists sounds stored on it (only some cameras do) offers them when
you browse its speaker. Those sounds also play on cameras on the WebRTC route.

### Status updates

While no live view is open, the integration keeps a read-only connection to
MyQ's video service. When a camera reports a change, its details are read again,
at most once a minute per camera. This connection never wakes a camera or opens
video, and polling continues alongside it.

## Recordings

Recorded clips are in **Media → MyQ recordings**, grouped by integration
entry, account and camera:

- Each clip shows a thumbnail, its local time, its length and detection labels
  (person, vehicle, package, animal, door).
- **Door events** lists clips started by the garage door opening or closing.
- **Older recordings** loads the next page.

What is available depends on MyQ's retention and your subscription. Clips
play through Home Assistant on a short-lived signed address, so the browser
never receives a MyQ token or cloud address. If a clip stops loading after a
long pause, select it again.

## Actions

The integration adds actions under `myq` (**Developer Tools → Actions**). They
need a Home Assistant administrator. Actions that target a device take its Home
Assistant device ID. Each change checks your MyQ role, is sent once, and waits
until MyQ reports it done; a pending or unconfirmed result is reported, not
retried. Actions that return data use Home Assistant's action responses.

| Action | Use |
| --- | --- |
| `get_device_history` | One page of a device's activity, oldest first, with a value for the next page. Not for guests. |
| `get_alerts` | The device's notification rules and whether each is on. |
| `snooze_alerts`, `cancel_alert_snooze` | Snooze all of the account's MyQ notifications for 1 minute to 7 days, or end the snooze. Owners and co-owners. |
| `rename_device` | Rename a device in MyQ (1 to 24 characters). Owner only. |
| `get_accessories` | List an opener's paired remotes and keypads, and start updating their battery sensors. |
| `rename_accessory`, `remove_accessory`, `assign_accessory` | Manage a paired accessory. Assigning needs an existing guest who already has access to the opener. |
| `set_auto_relock` | Turn a gateway lock's auto relock on or off, with its delay. |
| `set_auto_lock` | Set a Wi-Fi lock's automatic lock delay (30, 60, 90, 120 or 180 seconds), or 0 to turn it off. |
| `set_door_sensor` | Turn a Wi-Fi lock's calibrated door sensor on or off. |
| `get_pins`, `save_pin`, `delete_pin` | Manage a gateway lock's PINs. The PIN is read from a file; see below. |
| `get_guests`, `set_guest_devices`, `remove_guest` | List guests, change which devices a guest can use (keeping their time restrictions), or remove a guest. Unlike the app, these don't delete the guest's keypad PINs; remove those in the app first. |
| `get_schedules` | A camera feature's schedules and configuration IDs. |
| `save_schedule`, `set_schedule_enabled`, `delete_schedule` | Manage camera feature schedules (floodlight, patrol, privacy, voice deterrence). Times are `HH:mm:00` and weekdays lowercase names. Leave out the configuration ID only to create a feature's first configuration. |
| `get_thermostat_programs` | A thermostat's presets and schedules (read only). |
| `validate_session` | Check that the session works; see below. |

Use the IDs returned by the matching `get_*` action. Many of these have
entities too: the **Disable remotes**, **Auto relock**, **Automatic lock delay**
and **Door sensor** controls, **Alert** switches and the **Clear PIN lockout**
button.

### PIN file

`save_pin` reads the PIN from a file directly in Home Assistant's
configuration directory, so the PIN never appears in action calls, traces or
logs. Create it with permissions `0600`, owned by Home Assistant's user:

```json
{"pin": "123456"}
```

Give the file name, the PIN's display name, the lock, and the existing `pin_id`
when replacing a PIN. Links, other file types and files others can read are
refused. Delete the file afterwards; Home Assistant backups include files left in
its configuration directory.

### Session validation

`myq.validate_session`, given the integration entry's ID, reads the profile,
accounts, devices and roles. With `refresh: true` it also renews the session,
saves the new tokens and checks the account is unchanged. The result has counts
and roles, never credentials or identifiers, and it never operates a device.

### Not available

Device pairing and setup, inviting guests and changing roles, firmware
installation and Bluetooth-only lock operations are done in the MyQ app.

## Diagnostics and privacy

- **Diagnostics** (the device or integration's **Download diagnostics**) show
  device types, reported status, account roles, camera routes, each camera's
  power state (online, sleep mode and interval, battery, hardware model; a sleep
  mode the integration does not recognise is shown as the camera sent it), its
  speaker capabilities (talkback, codecs, stored sounds, and whether it has a
  Speaker), the result of the last live-video wake-up, the account's live-video
  feature flags, which video and audio modes a TC camera tried and which ones it
  used, the codec of its sound, how much of it was served and how long it took
  to begin, how many players were told of the sound when they started, and the
  last live-video error. They leave out tokens, names, serial numbers, account IDs,
  addresses and camera payloads.
- **Tokens** are stored in Home Assistant's configuration entry storage and are
  included in backups. Passwords, debug tokens and verification codes are not
  stored.
- **Video and images** are relayed through Home Assistant: the browser never
  receives a MyQ token, signed address or cloud media address. The integration
  saves no images or video itself.
- **Removing the integration** ends its MyQ session. The phone app and other
  sign-ins stay signed in.

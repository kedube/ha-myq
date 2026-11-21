# Cameras

Cameras need a working MyQ sign-in (see [sign-in](sign-in.md)) and camera
discovery switched on in the integration's options (it is on by default). Each
camera MyQ returns as its own device gets a camera entity. A camera built into a
garage door opener is listed under that opener.

All of this is implemented from the MyQ app's protocol and tested locally. It
has not yet been tried with a real MyQ camera.

## Snapshots

The camera entity fetches images the way the app does:

- **Battery cameras** reuse the camera's latest image, so they aren't woken for
  every snapshot. They take a fresh one if that image can't be loaded, has no
  capture time or is at least 24 hours old, but only while the battery is above
  10% and wake on motion is on.
- **Other cameras**, including opener cameras, ask for a fresh image and fall
  back to the latest one.
- **While a live view is open**, a snapshot is the newest frame of that view.

Images are cached for 60 seconds. The camera's `image_captured_at` attribute
shows when the image was taken, and `image_source` shows `cloud` or `live`.

To save an image, use Home Assistant's `camera.snapshot` action with a
destination your configuration allows:

```yaml
action: camera.snapshot
target:
  entity_id: camera.garage_camera
data:
  filename: /config/www/garage.jpg
```

## On, off and availability

A camera that can be switched off has a camera on/off control. It counts as off
only when it reports itself off; images are cleared while it is off. A sleeping
battery camera stays available even when MyQ shows it offline, because opening a
live view wakes it.

## Live video

Live video works on cameras that support MyQ's WebRTC route. Open the camera
entity, or a camera card with live viewing. Starting a view wakes a sleeping
camera first. Home Assistant relays the video and audio, which uses some CPU on
the Home Assistant host.

- **Several viewers:** up to four people can watch one camera at once. They
  share a single MyQ session, so MyQ sees one viewer. A fifth viewer, or one who
  joins while the session is still starting, is told the camera is busy.
- **Ending a view:** a session ends when the last viewer closes it, the camera
  is switched off, goes offline or is removed, the integration unloads, or ten
  minutes have passed since the session started. Reopen the camera to start a
  new one. A failed view does not reconnect by itself.
- **Not available:** permanent RTSP or HLS addresses, casting, and recording a
  live stream with `camera.record`. Older cameras that don't support the WebRTC
  route have snapshots and recordings only.

Home Assistant needs outbound HTTPS, access to MyQ's video service (Tend) on
port 5104, and WebRTC access to the AWS servers MyQ provides for each session.
Viewing from outside your network also relies on Home Assistant's normal WebRTC
setup.

## Camera controls

Buttons appear for the controls a camera advertises:

| Control | Notes |
| --- | --- |
| Pan, tilt, zoom and stop | Need an open live view. Each press moves one step, with a one-second stop safeguard. |
| **Go to …** saved positions | One button per position saved in the app. Work without a live view. |
| Sound and stop siren | Work without a live view. The siren stops by itself after ten seconds. |
| Enable and mute microphone | Need an open live view. The microphone mutes itself after 30 seconds. |

Commands that work without a view are sent over the open live view if there is
one, otherwise over the integration's background camera connection, otherwise
over a short MyQ video session opened just for that command. As in the app, any
account role can use these controls. These timers are safeguards on the Home Assistant side,
not confirmations from the camera.

Camera settings appear as switches, numbers and selects when the camera
advertises them: microphone, motion detection and sensitivity, speaker and siren
volume, image orientation, night vision, status LED, doorbell chime and chime
type, PTZ auto tracking, wake on motion, power saver, and camera lights and
their brightness. A sleeping camera is woken before a setting is changed.

## Two-way audio

The **MyQ live view** card adds talkback. It loads automatically; add it to a
dashboard and choose the camera and its two microphone buttons in the card
editor, or use YAML with your own entity IDs:

```yaml
type: custom:myq-live-card
entity: camera.garage_camera
microphone_on: button.garage_camera_enable_microphone_for_30_seconds
microphone_off: button.garage_camera_mute_microphone
```

**Start view** opens video and audio without a microphone. **Start with
microphone** asks for browser microphone permission and starts muted; hold
**Hold to talk** to speak. Releasing it, switching window or tab, the 30-second
limit or closing the view mutes the microphone. Browsers allow microphone
access only over HTTPS or on localhost. With several viewers, only the first
viewer's microphone can talk.

## Recordings

Recorded clips appear in **Media → MyQ recordings**, grouped by integration,
account and camera:

- Each clip shows a thumbnail, its local time, duration and detection labels
  (person, vehicle, package, door).
- **Door events** lists clips started by the garage door opening or closing.
  Door entries in the **Activity** event entities and in `get_device_history`
  link to the matching clip.
- **Older recordings** loads the next page.

Playback depends on MyQ's retention and your subscription. Home Assistant streams the clip through its own
short-lived signed address, so the browser never receives a MyQ token or cloud
media address. Re-select a clip if its playback link has expired.

## Status updates

While no live view is open, the integration keeps a read-only connection to
MyQ's video service for cameras you can view. When a camera reports a change,
its details are re-read (at most every ten seconds per camera). This connection
never wakes a camera or opens video; it pauses while a live view is open.
Regular polling continues as a fallback.

## Privacy

Video addresses, tokens and the temporary media server credentials are kept
out of entity attributes and diagnostics. The integration does not save images
or video itself.

For how the camera protocol works, see the [video reference](api/video.md).

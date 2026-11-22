# Devices and entities

The integration finds the devices in every MyQ account your sign-in can see.
Entities are created only for what MyQ actually reports for a device: a field
that is missing, or a capability the device doesn't advertise, gets no entity.
Devices added in the app appear on a later poll. A device that MyQ stops
returning for three full polls is removed from Home Assistant.

Being able to see a device does not mean you can control it. Shared accounts,
roles and subscriptions still decide what MyQ allows. Like the app, the
integration offers each setting and action to the account roles whose app
screens offer it (for example, opener and camera settings to the owner only),
and every write checks your current role first.

## Supported devices

| Device | Entities and controls | Not supported |
| --- | --- | --- |
| **Garage door openers** (`garagedooropener`, `wifigaragedooropener`) | Cover with open/close and door state. Owner settings: **Opener light timer**, **Time to close**, **Motion activated opener light** and **Disable remotes**. | Stop, position, direct work-light on/off |
| **Virtual (learned) doors** (`virtualgaragedooropener`) | Cover and door-sensor status. Monitor-only by default, as in the app. | Control while monitor-only |
| **Gates and commercial doors** (`gate`, `commercialdooropener`) | Cover with open/close. | Stop, position, pedestrian or hold-open modes |
| **Lamps** (`lamp`) | Light with on/off. | Brightness or colour |
| **Cameras** (garage, indoor, outdoor, battery, PTZ, floodlight, doorbell and keypad cameras) | Camera entity, settings, live controls and recordings. See [cameras](cameras.md). | Live video on cameras without MyQ's WebRTC route |
| **Wi-Fi and partner locks** (`wifilockgateway`, partner `*_lock` locks) | Lock with lock/unlock and state. For `wifilockgateway` owners: **Automatic lock delay** and **Door sensor**. | Bluetooth-only operations, partner-lock settings, modern credentials |
| **Gateway-connected locks** (`lockitron` and other `*_lock` types) | Lock with lock/unlock and state. **Auto relock** and **Auto relock delay** for owners. Lock PINs through [actions](integration-actions.md). | Bluetooth-only operations |
| **Linked thermostats** (types ending `_thermostat`) | Climate entity with the device's HVAC and fan modes, heating and cooling setpoints within its limits, and running state. Presets and schedules can be read through an action. | Changing presets or schedules, partner linking |
| **Gateways and hubs**, remotes, wall controls, pet doors and unknown types | Device-type sensor and any reported diagnostics. Paired opener accessories can be managed through [actions](integration-actions.md). | Controls, pairing, Wi-Fi setup |

Garage door commands go through the same checks the app makes before acting.
The door can't be controlled while it is offline, monitor-only, locked out after
wrong PIN attempts, reporting a work-light error, or (for a virtual door) its
sensor has a fault. Closing also needs MyQ's unattended-close permission, and
opening its unattended-open permission, when the opener reports them.

A thermostat can only be controlled while MyQ reports it as managed; a missing
value counts as managed, as in the app.

## Diagnostic entities

These appear when MyQ reports the underlying value:

- **Connectivity and power:** connectivity, offline reason, Wi-Fi signal,
  battery level, low battery, backup battery state and problems, and for cameras
  power source and charging.
- **Doors:** door state, last state change, door cycles, fault and fault codes,
  door sensor communication fault, and work light not paired.
- **Locks:** lock state, lock connection, door contact (**Door**), lock
  orientation, calibration problems, automatic-lock and door-sensor settings,
  and wrong-PIN lockout. A **Clear PIN lockout** button, disabled by default,
  is available to owners while a lockout is active.
- **Firmware:** an update entity for locks, openers and cameras that report
  firmware versions. It only shows that an update exists; installing firmware is
  not offered.
- **Every device:** a **Device type** sensor, which also lists the device's
  account role and its linked camera, opener or gateway devices.

Door cycles and battery levels keep long-term statistics.

## Activity, alerts and accessories

- **Activity** event entities (one per door, lamp and lock, disabled by default)
  fire an event for each new history entry while enabled. See
  [actions](integration-actions.md#management-actions).
- **Alert: …** switches turn each MyQ notification rule on or off.
- **Accessory battery** sensors for an opener's paired accessories are disabled
  by default and start updating after the `get_accessories` action.

## How the device list is organised

A Wi-Fi opener (`wifigaragedooropener`) and its built-in gateway
(`wifigdogateway`) share one Home Assistant device when MyQ explicitly links
them within the same account. The device uses the opener's name and model and
includes diagnostics from both records. Standalone hubs and gateways associated
with multiple doors remain separate.

Existing opener/gateway devices are combined automatically when the integration
loads or discovers the relationship. The opener's device ID, all entity IDs, and
entity customizations are preserved. Automations targeting the former gateway's
device ID need to target the combined device; entity-based targets keep working.

Other MyQ records remain separate devices. A camera built into a garage door
opener is listed under that opener. A device can only be deleted from Home
Assistant after MyQ no longer returns it.

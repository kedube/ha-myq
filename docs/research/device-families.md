# Device-family investigation

The record of the device-family analysis behind the [devices reference](../api/devices.md)
and the [devices guide](../devices.md). Detailed ledgers:
[doors, gates, lamps and accessories](devices-garage-evidence.md),
[locks](devices-lock-evidence.md) and [camera settings](devices-camera-evidence.md).

## Evidence and open questions

The base APK SHA-256 is
`20d6e5d35a819169867f1b7ebbe13769720ae0b78cb6e26ef95bc9f36dabefb2`.
This pass reused the earlier artifact inventory and the existing JADX 1.5.7
export, a local export kept outside the repository. The export had 558
decompilation errors, so incomplete coroutine/enum output is not treated as
runtime proof.

A private checkpoint (not in the repository) records source hashes and residual
questions; it is excluded from installation packages. Public ledgers summarize
behavior without distributing decompiled application code.
The remaining management trace is tracked as an open question.

Coverage status: device taxonomy and the implemented cloud dispatch paths are
statically answered; product applicability and all server/hardware behavior are
unverified; complete management parity remains partially answered. Missing
workflows include complete native role parity, pairing/provisioning, modern
credential and partner-lock configuration paths, thermostat schedules, broader
workflow provisioning, new guest invitations, advanced PTZ presets/patrol and
pet-door actuation. Supported camera schedules, bounded first-configuration
creation, legacy lock PIN/auto-relock, native Wi-Fi automatic-lock/door-sensor
settings, typed accessory/guest management, manual live controls, KVS talkback
and recorded event browsing/playback are implemented with local validation.
Authentication remains blocked on obtaining an accepted app-verified session.
The emulator's empty Play Integrity verdict is not a verified route to that
session. No hardware operation was attempted during this work.

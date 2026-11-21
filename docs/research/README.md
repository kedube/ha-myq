# Research notes

These records explain how the [API reference](../api/README.md) was built: what
was analysed, what was captured and tried, and what was tested. They are
history. The [user guides](../README.md#using-the-integration) and the API
reference are the current description; where a record disagrees with them, the
record is out of date.

## Records

| Record | Contents |
| --- | --- |
| [Sign-in investigation](sign-in-investigation.md) | Attempts to obtain an API session: hosted sign-in, the rejected token exchange, App Check, iOS, the emulator and browser-check trials |
| [Authentication review, November 20](auth-review.md) | Gap analysis of the sign-in implementation against the app, before the current implementation |
| [Protocol evidence](protocol-evidence.md) | Capture provenance, service hosts, and the door-control and camera investigations |
| [Validation record](validation.md) | Test runs and what each round of changes covered |
| [Capability coverage and open work](coverage.md) | Each app and API area, how far the integration covers it, and what remains |
| [App parity review](app-parity-review.md) | Review of control checks, statistics and management against the app |
| [Device-family investigation](device-families.md) | Evidence status for the device-family analysis |
| [Door, gate, lamp and accessory evidence](devices-garage-evidence.md) | Source ledger for openers and accessories |
| [Lock evidence](devices-lock-evidence.md) | Source ledger for locks |
| [Camera settings evidence](devices-camera-evidence.md) | Source ledger for camera capabilities and settings |
| [App architecture](app-architecture.md) | The analysed APK: identity, signing, native libraries, manifest, network and storage configuration |
| [App manifest inventory](app-manifest.json) | Parsed manifest declarations, with source line numbers |
| [Research tools](probe.md) | The account probe, the curl transport and APK route extraction |
| [iPhone capture helper](native-capture.md) | Observing the iPhone app's sign-in through a temporary local proxy |

## Provenance

The analysed artifact is MyQ Android 5.324.0.1132442, package
`com.chamberlain.android.liftmaster.myq`. The supplied XAPK and its base APK
were inventoried, the base APK and public configuration were extracted, and the
signing structure was inspected. The XAPK and base APK SHA-256 hashes are listed
under [Artifacts](sign-in-investigation.md#artifacts).

No repeated failed decompilation was treated as new evidence. Java feature
analysis used a local **JADX CLI 1.5.7** export, kept outside the repository;
that run exited 3 and reported 558 errors. The original inventory retains its
historical 1.5.6 metadata. The current re-extraction reproduced its route
declarations from the 1.5.7 output.
Readable methods and exact paths are cited; incomplete coroutines and enum
reconstruction warnings constrain conclusions.

Supplemental observations use Android SDK build-tools 36.0.0:

```text
aapt2 dump xmltree <base.apk> --file AndroidManifest.xml
aapt2 dump xmltree <base.apk> --file res/xml/network_security_config.xml
aapt2 dump xmltree <base.apk> --file res/xml/backup_rules.xml
aapt2 dump xmltree <base.apk> --file res/xml/data_extraction_rules.xml
apksigner verify --verbose --print-certs <base.apk>
```

The research checkpoint is private (not in the repository). Earlier private
checkpoints preserve authentication, door, camera and device-family
findings. Private bundles, raw captures, credentials, app media and decompiled
source are excluded from the public reference package. JSON inventories contain
public artifact declarations and source hashes, not values from a user account.

## Open questions

| Question | Current status |
| --- | --- |
| Which artifact and declarations were inspected? | Answered within the selected APK/splits and retained export |
| How does Android authentication compose its requests? | Statically traced; browser/MFA portions corroborated historically |
| Can this environment obtain an accepted native session? | Unresolved; no genuine accepted proof/session available |
| How are established door/lock/lamp/HVAC actions encoded? | Statically traced and implemented/tested locally |
| Are all roles, products and model-specific settings covered? | Partially answered; full account/entitlement semantics remain incomplete |
| How does the recovered KVS live path work? | Statically traced and tested locally; physical camera acceptance unverified |
| Are all API interfaces documented? | All 464 declarations in the bounded inventory are indexed; dynamic/native/backend-only surfaces remain partial |
| Is app management parity complete? | No: scoped management, supported camera workflow creation/schedules, recorded playback and live controls are implemented; broader provisioning, credentials/invitations, partner-lock settings, thermostat schedules, advanced camera operations and pet-door actuation remain incomplete |
| Is Android 5.325 or the iOS implementation equivalent? | Not established |
| Have physical actions or private media been validated? | No |

The remaining questions are preserved rather than filled with guessed endpoints,
fabricated tokens or unobserved state transitions.

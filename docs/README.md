# Documentation

The documentation is split three ways, so that what the integration does, what
is known about MyQ's API, and how that was found out stay separate.

## Using the integration

| Guide | Contents |
| --- | --- |
| [Sign-in](sign-in.md) | Email/password setup, verification, reauthentication, session renewal and removal |
| [Devices and entities](devices.md) | Supported device families, the entities each one gets, and what is not supported |
| [Cameras](cameras.md) | Snapshots, live video, camera controls, two-way audio and recordings |
| [Actions](integration-actions.md) | The `myq.*` actions, the optional entities they enable, and the live view card |

## MyQ API reference

[The API reference](api/README.md) describes MyQ's services as recovered from
the Android app: authentication, accounts and devices, cameras and live video,
recordings, live controls, management and the full route catalog. It states what
is known and cites the app's source locations. None of it is an official MyQ
specification, and most of it has not been confirmed against a live account.

## Research notes

[The research notes](research/README.md) record how the reference was built: the
sign-in investigation and its results, dated reviews, evidence ledgers, test
records, and the tools used for captures and probes. They are history, kept for
reproducibility; the guides and reference above are the current description.

## Development

[Development](development.md) covers the local environment, tests, packaging
and the repository layout.

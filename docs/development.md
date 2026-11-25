# Development

- [Environment](#environment)
- [Checks](#checks)
- [Packaging and releases](#packaging-and-releases)
- [Layout](#layout)
- [Ground rules](#ground-rules)
- [Research tools](#research-tools)

## Environment

The test environment uses Python 3.13 and Home Assistant 2026.2.3.
`hacs.json` declares Home Assistant 2026.2.0 as the minimum supported release.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --group test
npm ci
```

`--group` needs pip 25.1 or later. The `test` group in `pyproject.toml` holds
Home Assistant, pytest, ruff, mypy and the runtime dependencies; add
`--group browser` for the opt-in browser tests. Don't install the project
itself: the tests import the integration from the checkout, and an editable
install gives `custom_components` a path entry that Home Assistant's loader
cannot read.

## Checks

The same checks CI runs:

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
npm run typecheck
npm test
```

Use a current Node LTS release. The sidebar and door card tests run under Node
with jsdom (`tests/frontend/`); the pytest run includes the live card's Node
tests (`tests/test_live_card.mjs`). Browser sign-in tests open Chrome, so they
are opt-in:

```sh
MYQ_BROWSER_TESTS=1 .venv/bin/python -m pytest -q tests/test_browser_auth.py
```

Tests use synthetic MyQ responses and local HTTP, TLS, UDP and WebRTC peers.
`tests/test_tend_end_to_end.py` runs a TC camera's live video from a simulated
Tend service and camera through Home Assistant's stream worker.

On GitHub, the Validate workflow also runs HACS and hassfest on every push,
nightly and before a release. It also checks that Home Assistant can install
the manifest's requirements under its own package pins, for the minimum version
in `hacs.json` and the latest release; a conflict there makes adding the
integration fail with a 500 error. That check needs network access and `uv`,
and runs locally too:

```sh
.venv/bin/python scripts/check_requirements.py            # minimum and latest
.venv/bin/python scripts/check_requirements.py 2026.10.4  # specific versions
```

## Packaging and releases

```sh
.venv/bin/python scripts/package.py
```

This writes two archives to `dist/`, each with a `.sha256` file:

- `myq.zip`: the Home Assistant component, which HACS extracts into
  `custom_components/myq`. It leaves out the desktop-only modules
  (`api/browser.py`, `api/har.py`).
- `myq-<version>.zip`: the component, the research scripts, the README,
  changelog, license and `docs`.

For a private installation that includes an App Check registration:

```sh
python scripts/package.py --private-registration myq_app_check.json
```

This writes `analysis/private/install/myq-install-private.zip` with mode `0600`,
containing `custom_components/myq/` and `myq_app_check.json`. It stays out of
`dist/`, which releases publish, and public archives never include a
registration file.

### Releasing

Releases are manual: **Actions → Release → Run workflow** on `main`, or
`gh workflow run release.yml -f bump=minor`.

| Kind | Effect |
| --- | --- |
| `minor` (default) | Adds 0.01: 0.09 → 0.10 → … → 0.99 → 1.00 |
| `major` | The next whole number: 0.37 → 1.00, 1.04 → 2.00 |

Versions are `MAJOR.NN` and are the repository's tags. The workflow runs the CI
and Validate checks, writes the version into `manifest.json`, `pyproject.toml`
and `api/const.py`, moves the changelog's **Unreleased** section under the
version, commits and tags on `main`, and publishes a GitHub release with that
section as its notes and both archives attached. Running it again after a
failure past the tag publishes the same version. `scripts/release.py` does the
version and file updates.

Keep the changelog's **Unreleased** section current; it becomes the release
notes. The workflow pushes to `main` with `GITHUB_TOKEN`, so a protected `main`
must allow GitHub Actions to push.

## Layout

| Path | Contents |
| --- | --- |
| `custom_components/myq/` | Home Assistant platforms, config flow, coordinator, actions, media source, diagnostics |
| `custom_components/myq/frontend/` | Sidebar panel, dashboard door card, door artwork and UI translations (registered by `panel.py`) |
| `custom_components/myq/www/` | The live view card |
| `custom_components/myq/api/` | The MyQ client, independent of Home Assistant |
| `scripts/` | Packaging, release, requirement checks and research tools |
| `tests/` | Client unit tests and Home Assistant tests that load the integration |
| `docs/` | User documentation and the [API reference](api.md) |

The client in `api/`:

| Area | Modules |
| --- | --- |
| Sign-in and sessions | `auth.py` (hosted pages), `oauth.py` (PKCE), `tokens.py` (exchange, renewal, revocation), `firebase.py` and `app_check.py` (App Check) |
| Transport | `http.py`, `curl_http.py` and `android_tls.py` (native requests with Android's TLS profile), `backoff.py` |
| Accounts and devices | `client.py` (discovery and commands), `models.py`, `garage.py`, `locks.py`, `climate.py`, `account_access.py`, `associations.py` |
| Management | `management.py`, `alerts.py`, `history.py` |
| Cameras | `video.py` (REST, snapshots, wake-up), `camera_settings.py`, `camera_commands.py`, `recordings.py` |
| Tend signaling | `tend.py` (Cxnet framing and the mode-12 session), `status.py` (idle status connection), `live_controls.py` |
| TC live video | `tend_legacy.py` (video mode negotiation and the camera's audio connection), `tend_rtp.py` (UDP receiver), `tend_channel.py` (TLS media channel), `tend_source.py` (loopback stream source), `mpegts.py` (transport stream writer), `live_audio.py` (camera sound as Opus), `tend_audio.py` (talkback and stored sounds) |
| WebRTC live video | `kvs.py` (KVS signaling), `live.py` (aiortc relay to browsers) |

Live video takes one of two paths in `camera.py`:

- **TC cameras** (`MyQTendCamera`) are a Home Assistant stream source.
  `TendSource` serves an MPEG transport stream on a random loopback HTTP path:
  the camera's H.264 records, one picture to each PES, with start codes written
  the way FFmpeg writes them, and the camera's sound as Opus (`LiveAudio`
  re-encodes G.711). Every reader starts at its first keyframe, led by the PAT,
  PMT, SPS and PPS. go2rtc reads only the first PMT, so the PMT lists the audio
  while the camera's audio connection is still opening, and the audio carries
  20 ms Opus silence until the camera's sound continues its timeline. go2rtc and
  the stream worker both read the stream. The source owns the
  camera session, shares it among readers, and closes it 15 seconds after the
  last one leaves. A live view's session also holds the camera's audio
  connection, which the speaker's talkback shares.
- **WebRTC cameras** (`MyQCamera`) answer Home Assistant's WebRTC offers
  themselves: `LiveSession` opens the KVS session with an aiortc peer and relays
  it to up to four browser peers.

## Ground rules

- **Clean room.** The integration is written from observed protocol behaviour.
  Don't copy decompiled app code, or another integration's code, into the
  repository. The panel, door card and door artwork are Katherine Dubé's UI,
  reused with her permission.
- **Keep private material out of git.** APKs, decompiled sources, HAR files,
  tokens, captures and registration files stay outside the repository or in
  `analysis/private/` (see `.gitignore`).
- **No live side effects in tests.** Use synthetic responses and local peers;
  no test may contact MyQ or operate a device.

## Research tools

Command-line tools for investigating MyQ's sign-in and API are separate from
the integration. The account probe reads device data without operating devices;
the capture helper writes private authentication evidence. Install the desktop
tools in their own environment, since they import the installed package:

```sh
python3.13 -m venv .venv-tools
.venv-tools/bin/python -m pip install '.[browser,curl]'
```

### Account probe

`scripts/probe_account.py` signs in, then reads the profile, accounts, devices
and camera capabilities without changing anything. It ends with a
`SANITIZED REPORT` that is safe to share.

```sh
.venv-tools/bin/python scripts/probe_account.py --browser --app-check
```

`--browser` opens an isolated Chrome window for MyQ's sign-in pages and captures
only the return to `com.myqops://android`. `--browser-har` instead prints an
authorization address to open in your own Chrome, and reads a HAR exported
after the redirect. `--app-check` asks for an App Check token at a hidden prompt;
`--app-check-file` reads one from a file. Other options:

| Option | Effect |
| --- | --- |
| `--browser --connection-check` | Stop once the sign-in page loads |
| `--refresh` | Test an existing refresh token, entered at a hidden prompt |
| `--http` | Use the plain HTTP form transport |
| `--curl` | Use `curl_cffi` for HTTP |
| `--curl-profile chrome` | Use the desktop Chrome profile instead of Android Chrome |

### iPhone capture

`scripts/capture_native.py` observes the sign-in of your own MyQ iPhone app
through a temporary local mitmproxy. Only `partner-identity.myq-cloud.com` and
`firebaseappcheck.googleapis.com` are decrypted, and the report keeps only
selected OAuth fields, statuses and App Check tokens.

```sh
python -m venv analysis/private/capture-venv
analysis/private/capture-venv/bin/python -m pip install 'mitmproxy>=12,<13'
analysis/private/capture-venv/bin/python scripts/capture_native.py --prepare
analysis/private/capture-venv/bin/python scripts/capture_native.py \
  --session <directory printed by --prepare> --listen-host <Mac's local IP>
```

On the iPhone:

1. **Settings → Wi-Fi → (i) → Configure Proxy → Manual**: the Mac's address,
   port 8088.
2. Open the local address the helper prints in Safari, download the
   certificate, and install the profile.
3. **Settings → General → About → Certificate Trust Settings**: trust it.
4. Sign in to MyQ. Share only `sanitized-report.json`.

The capture stops after 15 minutes (`--minutes` to change). `--passthrough`
restores the phone's connection without decrypting anything, and
`--firebase-only` decrypts only Firebase. Afterwards, turn the proxy off, remove
the certificate profile, and delete the capture directory: it holds the
certificate's private key and any captured tokens.

### APK route extraction

With JADX installed, `scripts/analyze_apk.py` lists the HTTP routes the app
declares:

```sh
.venv-tools/bin/python scripts/analyze_apk.py '/path/to/MyQ.xapk' \
  --work-dir /path/outside/the/repository
```

It writes `analysis/api_inventory.json` by default. Keep the work directory
outside the repository; the inventory holds route facts and source locations,
not app code.

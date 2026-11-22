# Development

## Environment

The project targets Python 3.13 and Home Assistant 2026.2.3.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --group test
npm ci
```

`--group` needs pip 25.1 or later. The `test` dependency group (in
`pyproject.toml`) holds Home Assistant, pytest, ruff, mypy and the runtime
dependencies; add `--group browser` for the opt-in browser tests. The project
itself is deliberately not installed: the tests import the integration from the
checkout, and an editable install (`pip install -e .`) gives `custom_components`
a path entry that Home Assistant's loader cannot read, which fails every test that
loads the integration.

The optional extras (`browser`, `curl`, `probe`) are for the desktop sign-in
helper and research tools, which run from their own environment (see the
[research tools](research/probe.md)).

## Checks

These are the same checks CI runs:

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
npm run typecheck
npm test
```

Use a current Node LTS release. The sidebar and door card's browser-module tests
run under Node with jsdom (`tests/frontend/`); TypeScript checks their JavaScript
without a build step. The pytest run also includes the separate live card's Node
tests (`tests/test_live_card.mjs`). Browser sign-in tests are opt-in because they
open Chrome:

```sh
MYQ_BROWSER_TESTS=1 .venv/bin/python -m pytest -q tests/test_browser_auth.py
```

All tests use synthetic MyQ responses and local HTTP, TLS and WebRTC peers. No
test contacts MyQ or operates a device.

On GitHub, the Validate workflow also runs HACS's and Home Assistant's hassfest
checks on every push, nightly and before a release; they cannot run locally.
The same workflow checks that Home Assistant can install the manifest's
requirements under its own package pins, for the minimum version in `hacs.json`
and the latest release. A conflict there makes adding the integration fail
with a 500 error, and the CI tests can't see it because they pin one Home
Assistant version. This one needs network access and `uv`, and also runs locally:

```sh
.venv/bin/python scripts/check_requirements.py            # minimum and latest
.venv/bin/python scripts/check_requirements.py 2026.10.4  # specific versions
```
`tests/test_translations.py` covers what hassfest checks in the translation
files: every language has the English keys and placeholders, and no URLs or HTML.

## Packaging and releases

```sh
.venv/bin/python scripts/package.py
```

This writes two archives to `dist/`, each with a `.sha256` file:

- `myq.zip` is the Home Assistant component, which HACS extracts into
  `custom_components/myq`. It leaves out the desktop-only modules
  (`api/browser.py`, `api/har.py`). The curl transport is now a runtime dependency.
- `myq-<version>.zip` is the full bundle: the component, the sign-in
  helper and probe scripts, the README, the changelog, the license and the
  `docs` folder.

The component includes its icon in `brand/` (a neutral garage drawing; MyQ and
Chamberlain marks are not used).

For a private installation that includes an existing App Check registration:

```sh
python scripts/package.py --private-registration myq_app_check.json
```

This creates `analysis/private/install/myq-install-private.zip`, with mode `0600`.
Extract it at Home Assistant's configuration root: it contains
`custom_components/myq/` and `myq_app_check.json`. The private ZIP stays outside
`dist/`, which the release workflow publishes. Public archives exclude registration
files even if one is accidentally placed inside the component or documentation.

CI builds both archives on every push, so a packaging problem shows up before a
release.

### Releasing

Releases are manual. In GitHub, open **Actions → Release → Run workflow** on
`main` and choose the kind of release, or run
`gh workflow run release.yml -f bump=minor`:

| Kind | Effect |
| --- | --- |
| `minor` (default) | Adds 0.01: 0.01 → 0.02 → … → 0.99 → 1.00 → 1.01 |
| `major` | Goes to the next round number: 0.37 → 1.00, 1.04 → 2.00 |

The first release is 0.01 (or 1.00 if it is a major one); until then the files
carry 0.00. Versions are `MAJOR.NN`, and the released versions are the
repository's tags (`0.01`, `0.02`, …). Home Assistant and HACS compare the two
numbers, so 0.10 is newer than 0.09.

The workflow runs the CI and Validate checks, then writes the version into
`manifest.json`, `pyproject.toml` and `api/const.py`, files the changelog's
**Unreleased** section under the version, commits and tags that on `main`, and
publishes a GitHub release with that section as its notes and both archives
attached. If a run fails after pushing the tag, running it again publishes that
same version. `scripts/release.py` does the version arithmetic and file updates;
`tests/test_release.py` covers it.

Keep the changelog's **Unreleased** section up to date as changes land; it
becomes the release notes. The workflow pushes to `main` with the built-in
`GITHUB_TOKEN`, so if `main` is protected, allow GitHub Actions to push to it.

## Layout

| Path | Contents |
| --- | --- |
| `custom_components/myq/` | Home Assistant platforms, config flow, coordinator, services, media source and the live card (`www/`) |
| `custom_components/myq/frontend/` | Katherine Dubé's sidebar panel, dashboard door card, SVG door scenes and UI translations; adapted to the current entity registry by `panel.py` |
| `custom_components/myq/api/` | The MyQ client, independent of Home Assistant: sign-in (`oauth`, `tokens`, `auth`, `app_check`, `handoff`), HTTP transport, device families, cameras (`video`, `tend`, `kvs`, `live`, `status`), recordings and management |
| `scripts/` | The browser sign-in helper, the read-only account probe, the iPhone capture helper, APK route extraction and packaging |
| `tests/` | Unit tests for the client and Home Assistant tests that load the integration |
| `docs/` | User guides; `docs/api/` the MyQ API reference; `docs/research/` investigation records |

## App Check providers

`api/app_check.py` defines where sign-in gets its App Check token:

- `FirebaseDebugAppCheckProvider` (`api/firebase.py`): HA's default. Reads private
  registered credentials from `myq_app_check.json` and exchanges them with
  Firebase, checking app/audience and TTL before caching the proof.
- `SuppliedAppCheckProvider`: a token supplied for one sign-in.
- `FileAppCheckProvider`: a JSON or raw token file kept current by an external
  source. It reloads the file when the cached token gets stale or is rejected;
  it does not renew the token itself.
- `CachedAppCheckProvider`: wraps an asynchronous loader, with a timeout,
  one acquisition at a time, expiry checks and redacted error categories.

Each requires more than five minutes of validity. The token's expiry is read
locally as a freshness hint only; MyQ verifies authenticity. The token is used
only for the sign-in code exchange and is never saved.

## Ground rules

- **Clean room:** the integration is written from observed protocol behaviour.
  Do not copy decompiled app code into the repository; cite source locations
  instead, as the API reference does. The panel and door artwork are reused
  from Katherine Dubé's own UI with her express permission; that permission
  does not extend to the reference checkout's authentication code.
- **Keep research outside the repository.** APKs, decompiled sources, HAR files,
  tokens and captures stay out of git (see `.gitignore`); `analysis/private/` is
  for local notes only.
- **No live side effects in tests.** Use synthetic responses and local peers.

Research tooling (the account probe, iPhone capture and APK route extraction)
is described in the [research notes](research/README.md).

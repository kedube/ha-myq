# Research tools

Command-line tools used to investigate MyQ's sign-in and API. They are separate
from the Home Assistant integration. None of them sends a door, lock or camera
command, and none of them saves tokens.

Install them from a repository checkout:

```sh
python3.13 -m venv .venv-tools
.venv-tools/bin/python -m pip install '.[browser,curl]'
```

Keep this environment separate from the development one: the tools import the
installed package, while the tests import the checkout (see
[development](../development.md#environment)).

## Account probe

`scripts/probe_account.py` signs in, then reads the profile, accounts, devices
and camera capabilities without changing anything. At the end it prints a
`SANITIZED REPORT`, which identifies the failing phase without credentials; share
only that part.

### Sign in with interactive Chrome

```sh
.venv-tools/bin/python scripts/probe_account.py --browser --app-check
```

Google Chrome must be installed. The probe opens an isolated Chrome window where
you enter your password and verification code on MyQ's own pages, and complete
any browser check. It waits up to ten minutes and captures only the OAuth return
to `com.myqops://android`. It does not export cookies, record page bodies or
generate Android attestation.

### Sign in with your normal Chrome profile (HAR)

```sh
.venv-tools/bin/python scripts/probe_account.py --browser-har --curl --curl-profile chrome --app-check
```

The probe prints an authorization address. In your normal Chrome profile, open
Developer Tools → Network, enable **Preserve log**, clear the log, then open the
address. Complete sign-in and export a new HAR after the redirect to
`com.myqops://android`; an "app can't be opened" error is expected. Give the
file's path at the prompt within ten minutes. The default sanitized HAR export
is enough; cookies are not needed.

The probe owns the PKCE verifier for that address and checks the captured
callback's origin and state. It ignores captured passwords, cookies and token
responses. An older HAR, or a sign-in that ends at `/Home/Secure`, cannot
complete the transaction.

### Supplying an App Check token

`--app-check` asks for the token at a hidden prompt right after authorization.
`--app-check-file /path/to/proof.token.json` reads a JSON or raw token file,
checks it before sign-in and reads it again near the exchange. Both need more
than five minutes of validity. The token is sent only in the
`Firebase-AppCheck-Token` header of the code exchange; it is never stored or
reused. Don't put tokens in command arguments or chat. Browser sign-in without a
token source stops before opening a window.

### Other modes

| Option | Effect |
| --- | --- |
| `--browser --connection-check` | Stop once the sign-in page loads; no credentials asked |
| `--refresh` | Test an existing refresh token entered at a hidden prompt |
| `--http` | The legacy HTTP form transport |
| `--curl` | Use `curl_cffi` for HTTP; with `--connection-check`, test whether the browser check appears |
| `--curl-profile chrome` | Use the desktop Chrome profile instead of Android Chrome |

The curl transport uses the selected browser's TLS/HTTP profile and an
in-memory cookie jar for hosted sign-in pages. Native token and API requests
use a separate session with Android request metadata, no browser impersonation
and no cookies. This does not reproduce Android's TLS stack or supply App Check
proof; see the [native request profile](android-request-profile.md).
The transport does not run JavaScript, and its browser profiles have not passed
MyQ's browser check; see the
[sign-in investigation](sign-in-investigation.md#browser-verification) for the
recorded trials.

### Report contents

Reports include the selected public `native_request_profile` headers.
Token failures include a `token_exchange` section: HTTP status, recognised OAuth
and MyQ error codes, response format and whether App Check was supplied. Raw
server text and credentials are left out. `401.122` is reported as an
application verification failure, not a wrong password.

A 429 with `cf-mitigated: challenge` keeps the browser open for verification. A
429 without that marker ends the probe without retrying. API requests use the
`certifi` certificate bundle with TLS verification on.

## iPhone capture helper

`scripts/capture_native.py` observes selected sign-in exchanges from your own
MyQ iPhone app through a temporary local proxy. See the
[iPhone capture guide](native-capture.md).

## APK route extraction

With JADX installed, `scripts/analyze_apk.py` rebuilds the route inventory:

```sh
.venv-tools/bin/python scripts/analyze_apk.py '/path/to/MyQ.xapk' \
  --work-dir /path/outside/the/repository \
  --output docs/api/api_inventory.json
```

Keep the work directory outside the repository. The inventory records route
facts and source locations, not app code.

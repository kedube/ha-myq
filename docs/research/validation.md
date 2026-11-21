# Validation record

November 20, 2026 review fixes (0.1.10): **700 tests passed** across the default
suite (`.venv/bin/python -m pytest -q`: 694 passed, six opt-in browser tests
skipped) and the browser supplement
(`MYQ_BROWSER_TESTS=1 .venv/bin/python -m pytest -q tests/test_browser_auth.py --timeout=45`:
six passed). Ruff lint and format checks pass for all **91 Python files**.

Eighteen added regression cases cover cancellation while cleanup waits for a
command lock or sends siren OFF, concurrent session-close callers, delayed
settings reads after accepted/uncertain writes, stale unsupported responses,
and PIN replacement confirmation. Cleanup survives caller cancellation and is
shared by later callers. Cache revisions reject delayed pre-mutation readback.
Existing PIN replacements require a fresh pending-to-synced observation; new
PIN identities retain immediate-sync support. Validation used synthetic MyQ
responses, local HTTP/TLS/WebRTC peers and intercepted browser requests only.

November 20, 2026 implementation update (0.1.9): **682 tests passed** with
`MYQ_BROWSER_TESTS=1 .venv/bin/python -m pytest -q --timeout=45`.
Ruff lint and format checks pass for all **91 Python files**. Local socket access
was approved for synthetic HTTP/TLS/WebRTC and browser tests. No live MyQ service
or physical device was contacted.

The update fixes camera mutation locking, offline/privacy gates, uncertain
outcomes and per-origin quota handling. Tests establish that optional camera or
opener limits preserve newly observed door state, and that malformed privacy
state cannot expose a cached image. The snapshot API reference now names the
actual recent-image route and refresh query.

New coverage includes native account roles/grants and same-account associations;
remote PIN synchronization; guest access and accessory management; both supported
auto-lock configuration paths; calibrated native lock sensors; and camera workflow
schedule lifecycle, including first configurations with explicit feature gates.
HA tests exercise typed administrator actions, registry targeting, unloaded/removed
devices, service removal, and private PIN-file input that keeps numeric PINs out of
the HA service event bus. A session validator verifies identity and optional token
rotation/persistence without device commands.

Recorded-video tests browse synthetic events and play HA-signed HLS manifests,
segments and byte ranges through the real local HA HTTP stack. They cover expired,
tampered or revoked grants, same-account event ownership, untrusted origins,
manifest URI rewriting and exclusion of MyQ credentials from CDN requests.

Live-control tests verify PTZ/siren bytes, bounded local stop/mute behavior,
revoked permissions and cleanup after an uncertain socket write. Real local peers
verify microphone audio forwarding only after explicit activation. The bundled
Lovelace card has seven Node lifecycle cases, invoked by pytest, for microphone
consent, focus loss, release races, denied input, disconnects and late cleanup.
The static asset is also requested through the real HA HTTP server.

Read-only idle Tend monitoring is tested over local TLS: only login, keepalive
and logout are sent, with no video request or device-control message. Lifecycle
tests cover alias changes, empty inventories, fresh tokens after live-view pause,
bounded reconnects, auth failure, multiple live-view owners and unload. Camera
notifications trigger coalesced REST reads; they do not invent reported state.

A private checkpoint and provenance ledger (not in the repository) retain the
artifact identity, remaining questions and hashes for **272 reviewed source
files**, plus two focused modern-lock decompilation exports. The retained JADX
export and focused CLI recovery supplied code evidence.

**Remaining validation:** accepted MyQ authentication, physical controls, actual
recording entitlement/CDN behavior, media compatibility and timing. The emulator
integrity failure has not been solved. Full-account FCM still needs a genuine
receiver/forwarder; native provisioning, new guest invitations/role upgrades,
Seam automatic settings and legacy non-KVS talkback remain outside implemented
coverage. See the [action guide](../integration-actions.md) and the
[management](../api/management.md), [live-control](../api/live-controls.md)
and [recording](../api/recordings.md) evidence ledgers.

November 20, 2026 device-family update (0.1.8): **482 tests passed** with
`MYQ_BROWSER_TESTS=1 .venv/bin/python -m pytest -q --timeout=45`.
Ruff lint and format checks pass for all 70 Python files. The full suite includes
local HTTP/TLS/WebRTC and Chrome tests; local socket access was approved.

New coverage exercises both native lock routes, lock connectivity/permission
restrictions, thermostat modes/fan/setpoints/units/deadband/thresholds, residential
opener settings and support caching, and additional camera configuration/light
payloads. Real HA tests load lock/climate/select/light entities, dispatch services,
change permissions/capabilities and unload. Mutations are tested for exact payloads,
shared serialization, cancellation, nonreplay and absence of optimistic state.
Virtual openers now preserve the app's monitor-only default.

The source review reused the earlier artifact inventory and the existing JADX
export. A private checkpoint preserves five evidence records, two unresolved
questions and hashes of 58 reviewed source files.
See [device coverage and source ledgers](device-families.md) for the exact evidence and
remaining management features. No authenticated MyQ endpoint or physical device
was exercised; the App Check/session blocker remains unresolved.

November 20, 2026 camera implementation update (0.1.7): **327 tests passed**
with `MYQ_BROWSER_TESTS=1 .venv/bin/python -m pytest -q --timeout=45`.
Ruff lint and format checks pass for all 57 Python files. Local socket tests
ran with approved network access; they contacted only synthetic local peers.

New coverage includes fresh-snapshot query/authentication, camera-type discovery,
image coalescing/expiry/invalidation, power/wake-up conditions and timeouts,
Cxnet framing and Java Properties, verified local TLS login/start/accept/stop,
redirect restrictions, denied/truncated/disconnected signaling, and cancellation.
KVS tests use a local TLS WebSocket to exchange SDP and ICE, preserving the signed
query and excluding MyQ cookies/authorization. They check rejected redirects,
no automatic handshake retry, malformed messages and cleanup.

The media test uses actual aiortc peers: a generated H.264 video frame passes
through the integration's camera-facing peer and browser-facing peer to a
simulated viewer. It checks that peers and the native session close afterward.
HA tests exercise capability gating, the correct Tend alias/current token,
trickled ICE, one-viewer limits, failure/reopen behavior, startup cancellation,
privacy/offline/removal/capability changes and unload. They caught and fixed
startup/cleanup ownership races. A disconnected IM session closes media.

Additional environment: aiortc 1.14.0, PyAV 16.0.1, aioice 0.13.0 and aiohttp
3.13.3. PyAV matches HA 2026.2.3's stream requirement. `pip check` reports five
pre-existing ESPHome conflicts in inherited global packages (bleak, cryptography,
paho-mqtt, Pillow and voluptuous); none is introduced by the added WebRTC packages.
The integration test suite nevertheless passes in this environment.

No authenticated MyQ service or physical camera was contacted. The source
investigation covers APK 5.324.0.1132442; the user's camera capability, server
acceptance, real image freshness and sustained media remain unverified. See
[camera protocol evidence](protocol-evidence.md#camera-investigation--november-20-2026-017)
and [camera behavior and limits](../cameras.md).

November 20, 2026 garage-door verification update (0.1.6): **225 tests passed**,
including all six opt-in Chrome tests and real loopback HTTP/libcurl cases.
Ruff lint and format checks pass for all 48 Python files. The initial sandboxed
full run could not bind the local test servers; rerunning with local socket
access passed. No live MyQ endpoint or physical device was contacted.

The added coverage checks both open/close payloads and authorization headers,
independent action permissions, malformed monitor-only values, case-insensitive
and malformed door states, overlapping requests, cancellation, HTTP failures,
actual Home Assistant cover service dispatch, permission revocation, and
post-command refresh without assuming successful movement. Malformed state
data now stays unknown; malformed monitor-only values disable controls.

A real loopback test reproduced a hidden aiohttp 3.13.3 PUT retry: the server
received a simulated command twice after dropping its first response. The
fixed transport delivers it once and reports an uncertain outcome, including
when an outer middleware attempts a retry. Tests also establish preservation
of existing middleware and ordinary read behavior on the shared session, plus
rejection before send if its middleware interface is unavailable. Middleware
support requires aiohttp 3.12 or later. The implementation reads aiohttp's
private `_middlewares` attribute to preserve the existing chain, a documented
compatibility dependency; it does not mutate the HA-owned session.

See the garage-door verification section in [protocol evidence](protocol-evidence.md) for
the artifact inventory, JADX source trace, and live-validation limits.
The command protocol was reviewed against Android 5.324; neither the installed
5.325 command path nor actual opener operation has been verified.

Earlier November 20, 2026 authentication implementation update: **176 tests passed**, including all
six opt-in headless Chrome tests and the actual libcurl loopback cases:

```sh
MYQ_BROWSER_TESTS=1 .venv/bin/python -m pytest -q
ruff check custom_components scripts tests
```

The added checks cover supplied/file App Check proof, local expiry hints,
bounded reads and nonregular files, concurrent acquisition, cancellation,
cache invalidation, redacted errors, and proof/transaction expiry while waiting
for exchange. Real token-manager tests establish that local failures leave a
code unused and that concurrent completions, server rejections and cancellation
after sending cannot replay it. Native headers are checked on token/API requests
and excluded from browser navigation and camera requests.

At that checkpoint, HA flow tests covered the browser handoff, retention of the private verifier in
HA, rejected/stale handoffs, proof replacement without code consumption,
discovery retry without repeated exchange, same-account reauthentication, and
saving only the OAuth session and verified identity. A cleanup test exposed an
existing attempt to close an HA-managed connector; the flow now detaches its
short-lived session as required by the installed HA helper. Real Chrome tests
exercise the public handoff request against intercepted synthetic pages.

All credentials and responses used in these tests are synthetic. No live MyQ
login or physical command was attempted. There is still no working native
App Check producer or supplied genuine token, so server acceptance of the new
metadata/proof combination remains unverified. The providers consume external
proof; they do not mint it. The browser handoff was removed from setup on
November 21. Current flow tests cover direct email/password setup, MFA,
same-account reauthentication, error recovery and discovery retries; see the
[sign-in guide](../sign-in.md).

Environment: Python 3.13.7, Home Assistant Core 2026.2.3,
pytest-homeassistant-custom-component 0.13.316, and PyTurboJPEG 1.8.0.
Home Assistant's installed version is the test target, not a claim about the
user's installation. The user's Home Assistant version is still unknown.

Before the native capture helper was added, the local suite passed **126
tests** with the optional curl dependency installed, including 11 curl transport
cases. The capture helper has nine additional passing tests, recorded below.
Two curl cases use a loopback
HTTP server and actual libcurl; the rest use mocked I/O. Without curl installed,
its test module is skipped. Six additional opt-in tests exercise real Chrome
against intercepted synthetic pages. They cover:

- PKCE and OAuth state binding, same-origin forms, CSRF fields, synthetic
  password/MFA exchanges, method selection, incorrect-code retry, expiry,
  unsupported screens, and no POST replay on redirect.
- Correct unified versus legacy authorization parameters from the APK call
  chain, browser JavaScript/cookie behavior, synthetic MFA, server and scripted
  app callbacks, wrong-state rejection, and one-time code exchange.
- Correctly recognizing browser challenges at HTTP 403/429, keeping their pages
  open for user interaction, and distinguishing an actual retry response.
- Concurrent token refresh, refresh rejection, rotation persistence,
  same-account reauthentication, and no integration reload solely for a token
  change.
- Account/device decoding, multi-account discovery, partial-list detection,
  permission-gated commands, unavailable devices, no automatic command replay,
  and lamp/device-type separation.
- Camera JPEG validation, partial settings payloads, bounds/capabilities, and
  nullable or malformed camera sections.
- Bounded responses, rate-limit cooldowns, diagnostic error categories, and
  credential-safe exception messages.
- Actual curl cookie retention across redirects, form encoding, CSRF fields,
  synthetic password/MFA, code exchange, refresh rotation, authenticated profile
  reads, and aborting an oversized response without a CFFI traceback.
- The successful HAR's password/email-MFA route and field structure, using
  synthetic values; the SSO boolean lookup, its AJAX metadata, and refusing to
  treat a website landing page as an API token response.
- Fresh HAR callback extraction with origin/state/expiry checks, duplicate and
  ambiguous callbacks, stale transactions, malformed/oversized files, and no
  reuse of captured passwords, cookies, or token response bodies.
- Optional App Check header handoff through OAuth code exchange, no persistence
  or refresh reuse, malformed header rejection, allowlisted error diagnostics,
  and application rejection without replaying a code or blaming the password.
- Actual Home Assistant setup/unload, platform/entity creation, dynamic device
  changes, isolated camera outages, MFA-gated account creation, reauthentication,
  options changes, and redacted diagnostics with mocked cloud traffic.

The test data is synthetic. No test signs into MyQ, moves a door, changes a
camera setting, creates an invitation, or performs a purchase.

Native capture tooling was added separately on November 16, 2026. Six additional
synthetic tests passed for output redaction, exact host scoping, private App Check
storage, provider identification, redirect-query stripping, and token bounds.
The combined capture/token-exchange/HAR subset passed all 37 tests. Ruff passed
for all 43 Python files. An isolated mitmproxy 12.3.1 instance served its exact
public CA certificate over loopback, with no private key in the response, then
shut down cleanly. This validates the helper's local setup only; native iPhone
TLS acceptance and App Check acquisition remain unverified.
After the user reported a successful native-app login, the capture still had
zero auth events or App Check files. Safari on the phone displayed mitm.it's
message that traffic was not passing through the proxy. Connection diagnostics
were added to distinguish routing from decryption failures; all seven capture
tests passed, including exclusion of client addresses and unrelated hostnames.
The proxy was restarted with the same CA so no replacement certificate is needed.
Further checks found 35 incoming connections and active connections from another
local address. A controlled request through the correctly configured proxy
reproduced mitm.it's external "not going through" page over HTTPS, whereas HTTP
served the installation page. That message alone therefore did not establish
that the phone's proxy setting was wrong. The helper now serves onboarding at
its explicit local listening address. Direct and proxied local-page/certificate
requests returned HTTP 200 and the expected public certificate without private
key material. All eight capture tests passed. The phone's native login exchange
has still not been captured.
With interception enabled, native sign-out spun and returned to the app while
the proxy reported repeated client TLS handshake failures for a selected host.
No HTTP auth request was decrypted. The user disabled the phone proxy and
successfully signed out. This links the failure to interception; it does not
by itself distinguish missing certificate trust from native certificate pinning.
Recovery pass-through and Firebase-only capture modes were added. Credential-free
TLS checks verified that Firebase-only mode presents the actual server's chain
for MyQ identity and the local capture CA for Firebase App Check, with certificate
verification enabled and no HTTP requests sent. All eight capture tests still
pass. The next native login is intended to test only Firebase App Check traffic;
the native OAuth exchange is deliberately not decrypted in this mode.
The user completed a native login with Firebase-only capture enabled. The
capture recorded no selected HTTP exchange and saved no App Check token. The
Firebase CONNECT hostname in that report was already present from the Mac's
pre-login TLS check, so it is not evidence of a phone-origin Firebase exchange.
A cached token or a different traffic path remain possibilities, not findings.
A subsequent credential-free Mac GET to the Firebase service root through the
proxy verified the capture CA and returned the expected HTTP 404 page. A phone
Safari check of that same address was requested without another login attempt.
The user reported the expected 404, but the proxy's decrypted-request count
remained one (the Mac test). That result did not establish that the phone trusted
the capture CA: the same 404 is returned without interception. Consequently,
native certificate pinning remains a hypothesis, not a verified finding.
The helper now supplies an explicit local HTTPS marker at a diagnostic-only
Firebase path and counts whether that request arrived from another device.
Mac tests cover trusted proxy, untrusted proxy, and direct routing to distinguish
these outcomes. No native App Check token has been captured.
The read-only Xcode installed-app metadata query did not complete; its error
referenced Developer Mode and a timeout. No Developer Mode or phone permissions
were changed, and no native app data was read.
The phone subsequently reported Google's 404 for the diagnostic path, without
an observed remote diagnostic success. By the next inspection the 15-minute
proxy timer had elapsed and the listener had stopped; the exact timing of the
phone request relative to expiry is unknown. Installed mitmproxy code checks
both destination addresses and TLS SNI when applying the host allowlist, so
IP-address CONNECT alone is not an established explanation.
Run status/deadlines and an optional direct local HTTPS diagnostic were added.
The local diagnostic uses an IP-SAN certificate issued by the existing capture
CA. Mac tests returned its fixed success marker with that CA trusted and failed
certificate verification without it. Eight capture tests passed. The phone's
direct local HTTPS check remains pending; no App Check token has been obtained.
The user saw the certificate installer at the diagnostic address. Local
reproduction isolated a setup bug: the built-in onboarding app matched the
Mac's hostname on every port, so HTTP (rather than HTTPS) sent through the proxy
to port 8443 incorrectly returned the installer. Direct and proxied HTTPS both
returned the trust marker. Onboarding is now restricted to the proxy port, and
proxied HTTP on the diagnostic port redirects to HTTPS. Tests of all three paths
plus the installer confirmed the intended responses; all eight capture tests
passed. This does not yet establish the iPhone's certificate trust.
The user then reported the exact local HTTPS trust marker. The direct TLS
request counter increased from three Mac verification requests to five, with no
additional Mac requests in between. This establishes that the phone reached the
local HTTPS diagnostic successfully. Authentication inspection was resumed for
the original two hosts using the same CA. Client/upstream TLS failures now record
only fixed reason categories, host roles, and whether the connection came from
another device; raw errors and addresses are excluded. All nine capture tests
passed. A native authentication attempt after this trust confirmation is pending.
That attempt subsequently recorded the iOS authorization parameters documented
in `protocol.md`, with HTTP 429 and 302 authorization responses. Other phone
connections to the identity host failed client TLS, including two
`certificate_unknown` alerts. No token exchange or App Check token was captured.
The sanitized snapshot was preserved privately before restoring encrypted
pass-through. One credential-free iOS-client token diagnostic returned HTTP 401,
MyQ `401.122`, with no standard OAuth error and no retry. No working API session
has been obtained. Further password/MFA retries are not justified by this result.
The user subsequently removed the temporary certificate profile, disabled the
phone proxy, and successfully signed back into MyQ. The local helper was stopped
and ports 8088 and 8443 were verified to have no listeners. Normal native-app
access is restored; integration authentication remains unresolved.
Generated capture certificates and private keys were removed, and the temporary
capture session was closed. The private sanitized evidence snapshot remains
available for protocol provenance. No App Check token files were present.

Public network observations:

1. MyQ OpenID discovery returned HTTP 200 and advertised the expected endpoints
   and S256 PKCE.
2. The exact client authorization request followed three identity-origin 302
   redirects and loaded the password form with HTTP 200.
3. The local Python installation's default macOS CA file was absent. The
   standalone probe now explicitly uses certifi; the credential-free check
   passed with the shell's `SSL_CERT_FILE` override removed. Home Assistant
   continues to use its own managed TLS session.
4. The user reran the probe successfully through the public form. Submitting
   the password form returned HTTP 429. The original report did not include
   the Retry-After header, so its server-provided duration is unknown. New
   reports distinguish a server delay from a local fallback. No retry or
   physical command was sent automatically.
5. A later raw attempt received HTTP 429 on the first authorization GET, and
   the initial browser probe received 307 then 429 before password entry.
6. The public sign-in SSO JavaScript was inspected for additional request
   behavior. No authenticated MFA response or successful token exchange was
   observed. HTTP 429 is not evidence of an invalid password or proof of a
   specific app-attestation failure.
7. A subsequent anonymous response positively identified the immediate block:
   `Server: cloudflare`, HTML content, `cf-mitigated: challenge`, and no
   `Retry-After`. The earlier generic 429 handling incorrectly reported a local
   60-second fallback and closed the browser. The browser now renders that
   interstitial and waits for normal verification; the raw client reports a
   separate browser-verification error. No credentials were sent in this check.
8. Real Chrome tests use synthetic/intercepted pages, including a verification
   interstitial followed by a login page. They do not establish that Cloudflare
   will accept the user's browser session or that Android attestation is optional.
9. A credential-free comparison with `curl_cffi` 0.15.0 tested its default
   transport and `impersonate="chrome"` against the unified authorization URL.
   Both returned HTTP 429 with `cf-mitigated: challenge` and no `Retry-After`.
   Browser TLS/HTTP impersonation alone did not resolve the challenge in these
   attempts. No password, MFA code, or token was submitted, and `curl_cffi` has
   not been added as an integration dependency. These observations do not
   establish how a different browser session or a later request would behave.
10. On November 15, 2026, the user reported seeing the browser's security
    verification page and requested a further curl trial. The new optional
    `--curl --connection-check` path, using curl_cffi 0.15.0's Android Chrome
    profile (`chrome131_android`), returned HTTP 429 with the challenge marker
    on the first authorization GET. It reported `BrowserVerificationRequired`
    and stopped before password entry. Curl is now a development extra, not
    a Home Assistant runtime requirement. No credential or physical command
    was sent in this trial.
11. On November 16, 2026, the user supplied a HAR of successful website sign-in.
    Its 78 entries include the password POST, three email-domain SSO lookups
    returning boolean false, the email `Otp` page and successful MFA POST, and
    a final 200 at `/Home/Secure`. There are no app authorization or token
    requests, no custom-scheme callback, and no exported cookie headers.
    An offline comparison against the actual capture confirmed that the parser
    preserves all submitted fields and values and identifies the code challenge.
    No captured credential was sent over the network or copied into the repository.
12. A credential-free curl check following `/` then `/Home/Secure` reached the
    password form with HTTP 200 and no challenge using the desktop `chrome146`
    profile. A second check reached those pages again, then requested the app's
    unified authorization URL in the same session; that step returned a
    Cloudflare challenge with HTTP 429. The capture reports Chrome 157 on macOS;
    neither its browser fingerprint nor its initial cookie state can be
    reconstructed from this export. No conclusion about the cause of the
    different Cloudflare outcomes is established.
13. The user completed `--browser-har --curl --curl-profile chrome`. The probe
    validated its callback, then received HTTP 401 at token exchange. The second
    HAR contains the matching app authorization parameters and final redirect;
    it contains no token request because that request was made by the Python
    process after export. The original 401 body was discarded by the old generic
    error handling, so its provider code cannot be recovered. No real code was
    replayed during subsequent investigation.
14. Two bounded anonymous diagnostic exchanges, using an explicitly invalid
    authorization code and no user credentials, returned HTTP 401. The second
    inspected the recognized provider fields and found `401.122`, "Unauthorized",
    and "The application is unauthorized." APK handling distinguishes this
    from invalid OAuth grants and invalid refresh tokens. Missing App Check is
    a concrete difference from the normal app path; acceptance with a genuine
    token remains untested.
15. Both browser HARs were inspected for App Check headers and requests. None
    were present. The second HAR's Firebase scripts implement Analytics and
    Installations; generic SDK component-name strings are not evidence of App
    Check initialization. The user has neither a signed-in Android device nor
    a native-app capture available. The optional hidden `--app-check` input is
    implemented and tested with synthetic tokens, but no real token has been
    supplied and no token-acquisition/renewal mechanism is implemented.

16. On November 20, 2026, the user reported `401.122 (-998)` during normal myQ
    login on an Android emulator, before beginning proxy/certificate setup.
    Read-only ADB checks identified Android 17/API 37 (`REL`, `user` build),
    an ARM64 Google Play system image, myQ 5.325.0.1132545 with installer
    `com.android.vending`, and an unset global HTTP proxy. The inspected APK
    is the earlier 5.324 version; its `-998` mapping points to an App Check
    acquisition exception (see `auth-review.md`), but this mapping has not been
    independently established for the installed 5.325 binary. A bounded read
    of the running myQ process's main log, reduced in memory to fixed error
    markers, exposed no underlying Firebase HTTP status or attestation verdict.
    Raw logs were neither printed nor saved. No App Check token was acquired,
    no login was initiated by the agent, and no emulator trust/proxy settings
    were changed. The user subsequently confirmed that Play Store reports
    "Device is not certified", system updates report up to date, and the
    selected hardware profile is Pixel 9 Pro XL. This strengthens the device
    verification hypothesis without identifying MyQ's exact failed check.
    The user then ran Play Store's built-in Check integrity diagnostic and
    reported `Labels: []`. That test returned no device-integrity labels.
    Together with the App Check error and uncertified status, this supports
    an integrity-related blocker on this emulator. It is a separate Play
    Store diagnostic, not the decoded verdict from MyQ's own request, and
    does not establish MyQ's exact Firebase policy or prove that every
    emulator configuration fails. This configuration is not a verified
    source of usable App Check proof; proxy/certificate capture has not
    proceeded.

To continue validation, obtain a genuine MyQ App Check token source and establish
whether the token exchange accepts that proof. Browser password/email-MFA and
the app OAuth callback are already evidenced. A supplied token can be tested
using a fresh code and `--browser-har --app-check`; another browser-only HAR
does not provide the missing native attestation. After obtaining API tokens,
use the read-only probe to confirm account/device schemas and camera capabilities. The generated report
omits identity values and private camera payloads; credentials stay local.

After successful discovery, validate token refresh/restart behavior and then
perform a deliberate user-initiated open/close test in Home Assistant, comparing
reported state with the app. Validate camera reads before changing settings.
Live-video transport needs separate protocol work with an authorized camera
session. Until those steps are complete, this repository must not claim full
app/API parity or verified support for the target opener.

## Test coverage by area

What the local tests cover for each API area. Moved here from the API chapters.

### Cameras and video

Local tests cover fresh-image query/authentication, image caching and
invalidation, wake conditions/timeouts, Cxnet framing/Properties, actual local
TLS login/start/accept/stop, KVS WebSocket envelopes, signed-query preservation,
redirect rejection and cleanup. A local aiortc test sends a generated H.264
frame through both peers to a simulated viewer. HA tests exercise capability
gates, current credentials/alias selection, ICE, lifecycle races, privacy,
offline/removal and unload. Settings tests validate payloads and coupled-field
preservation. Added tests exercise HLS browsing/playback through HA's actual
signed HTTP routes, byte ranges and revoked grants, exact control frames,
generated G.711 uplink through local WebRTC peers, and browser microphone races.
These establish local behavior, not real camera compatibility.

The historical 0.1.8 suite had **482 passing tests**. Current 0.1.9 verification
is recorded in [validation](../research/validation.md), with feature-specific coverage in
the implementation ledgers above. The tested stack is HA 2026.2.3, Python 3.13,
aiortc 1.14.0 and PyAV 16.0.1; other HA/runtime versions remain unvalidated.

Public documentation contains no account credentials, private images, signed
session URLs, TURN secrets or user/device identifiers. Private evidence is
retained separately and excluded from installation packages.

### Recordings

The recordings tests use synthetic media and account data, and only a localhost listener:

- `tests/test_recordings.py` covers request parameters, pagination ties, ownership checks, HLS discovery and probing, URL validation and expiry, keeping credentials away from redirect targets, playlist rewriting, HA media browser objects, HA signature validation and grant revocation.
- `tests/test_recordings_extra.py` covers:
  - lenient rows and pagination past undisplayable pages
  - door-folder and `door_clips` requests and matching
  - labels and local-time titles
  - playback without a status gate, the videoCdn fallback and status hints
  - media-info caching and credential hosts
  - dropped renditions, sliding grant expiry and thumbnails through HA
  - progressive streaming over a real aiohttp session

### Live controls

Tests exercise exact synthetic command bytes, alias routing, permission/privacy/
calibration gates, bounded controls, stop ordering, unknown write outcomes,
microphone silence and timeout, and actual H.264/G.711 WebRTC peers over loopback.
The local audio test proves sound crosses the browser-to-camera bridge only after
explicit enable and stops after mute. Node tests use synthetic DOM, HA WebSocket,
microphone, and peer objects for permission races, denied controls, focus loss,
late subscription cleanup, signaling disconnect, and view-only behavior.

### Management

`tests/test_management.py` covers exact wire contracts, role changes, denied
grants, cross-account associations, empty workflow configurations, pending and
failed outcomes, non-replay, privacy and concurrent-operation rejection.

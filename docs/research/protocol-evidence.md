# Protocol evidence

See the [November 20 authentication review](auth-review.md) for fresh
artifact verification, JADX method inspection, native request metadata, and
the prioritized comparison with the current implementation.

Research input: `com.chamberlain.android.liftmaster.myq`, Android version
5.324.0.1132442, version code 1132442, supplied as an APKPure XAPK. Static
analysis used JADX 1.5.6. The original decompilation exited with status 3 and
produced usable sources with some decompilation failures. Reusing that output
in the inventory script records a null return code, because the script did not
run a new decompilation. This is an incomplete static analysis, not a complete
description of the service.

| Artifact | SHA-256 |
| --- | --- |
| Supplied XAPK | `77aee562ebbef96712ac44b41ee899047f3a1e92860fe6e32d760e5422c7d7f5` |
| Base APK | `20d6e5d35a819169867f1b7ebbe13769720ae0b78cb6e26ef95bc9f36dabefb2` |
| Supplied website-login HAR | `d9eb159b69d433c8b6ae3473cf449b64e7bb0a87d439ac0aa49aa7eaa5f85eb1` |
| Supplied OAuth-login HAR | `4cc92d30389a5b9ca3fda31d743c4cfe0f4cf69b740bd84254c8d8aecff3da88` |

These hashes identify the analyzed artifacts. The signing certificate was not
independently compared with the Play Store certificate. Source references below
are relative to JADX's `sources/`; the asset reference is under `resources/`.
No other integrations were inspected, and no decompiled source is distributed.

`assets/myq-config.json` supplies the production service suffix, public Android
OAuth client, scopes, and redirect URI. `hp0/e.java`, `ta2/g.java`, `oy0/b.java`,
and `nt0/d.java`/`nt0/e.java` connect those settings to the relevant service hosts.

| Purpose | Host | Evidence |
| --- | --- | --- |
| OAuth identity | `partner-identity.myq-cloud.com` | `hp0/e.java` |
| Account list | `accounts.myq-cloud.com` | `ta2/g.java` |
| Profile | `profiles.myq-cloud.com` | `ta2/g.java` |
| Device discovery | `devices.myq-cloud.com` | `oy0/b.java` |
| Device actions | `devices-action.myq-cloud.com` | `oy0/b.java` |
| Standalone lamps | `account-devices-lamp.myq-cloud.com` | `oy0/b.java` |
| Camera media/details | `media.hubs.tend-us.tendplatform.com` | `nt0/d.java`, `nt0/e.java` |

The public OpenID configuration and hosted password form were fetched without
credentials. The configuration confirms `/connect/authorize`, `/connect/token`,
and S256 PKCE. The public discovery document listing a password grant does not
establish permission for this particular OAuth client to use it. This client
implements the app's hosted authorization-code flow.
([MyQ OpenID configuration](https://partner-identity.myq-cloud.com/.well-known/openid-configuration))

The authorization request uses `client_id=ANDROID_CGI_MYQ`,
`scope=MyQ_Residential offline_access`, `response_type=code`,
`redirect_uri=com.myqops://android`, `code_challenge_method=S256`,
`ui_locales=en-US`, `acr_values=unified_flow:v1  brand:myq`, and `prompt=login`
for unified browser sign-in (the two spaces follow the app's empty signup slot).
The implementation also generates and verifies an OAuth `state` value. It
retains the PKCE verifier only for the sign-in flow.

On November 16, 2026, a scoped capture from the user's iPhone independently
observed `client_id=IOS_CGI_MYQ`, `redirect_uri=com.myqops://ios`,
`scope=MyQ_Residential offline_access`, and `code_challenge_method=S256` on
`/connect/authorize`. Responses included HTTP 429 followed by 302; that 302
alone does not establish completion of OAuth or receipt of a code. The capture
also recorded client TLS failures to the identity host, including
`certificate_unknown`, after a direct local HTTPS trust check succeeded on the
phone. Browser authorization traffic was decrypted, but no `/connect/token`
request or App Check value was obtained. This is consistent with native
app-specific trust restrictions; the implementation of any iOS pinning or
attestation provider has not been verified from the iOS binary.

The private sanitized snapshot `ios-authorize-and-tls-failures.json` has SHA-256
`57049f01edad13de2d0685d69fe4e349ac56da6d203f9ffe7601ff185a72a305`.
It contains only the observer's allowlisted metadata, not raw flows or credentials.
One subsequent token POST used the observed iOS client/redirect, a deliberately
invalid authorization code, and a synthetic PKCE verifier. It returned HTTP 401
with MyQ code `401.122`, matching the earlier Android diagnostic. No actual
authorization code was replayed, and no automatic retry was made. This does not
prove which specific iOS application-verification header or proof is required.
The integration's OAuth implementation remains Android-derived; iOS renewal and
token exchange are not claimed to work based on these observations.

The observed form posts `Email`, `Password`, `ReturnUrl`, and
`__RequestVerificationToken` to the form's `/Account/LoginWithEmail` action.
Cookies and hidden values come from the current page, rather than hard-coded
values. Only HTTPS identity-origin forms and redirects are accepted; the
registered custom-scheme callback is validated separately. No off-origin form
submission or POST replay through a 307/308 redirect is allowed.

The unified sign-in call chain is `sp0/b.java` (Android Custom Tabs) →
`cp0/d.java` → `ip0/j.java:c()` → `ip0/n.java:h()` →
`ap0/AuthUrlParams.java`, with `signUp=false`, `unifiedFlow=true`, and
`isMFA=false`. That produces `unified_flow:v1`. The bare `unified_flow` value
belongs to the authenticated MFA-settings flow (`ip0/j.java:q()`), where
`isMFA=true`. The initial implementation incorrectly conflated these paths.

`ip0/j.java:x()` is a separate hidden-WebView credential-injection path. Its
`j0()` calls `i0(false, false)` and generates `acr_values=  brand:myq`.
The remaining experimental HTTP form transport now uses that legacy parameter
set. Neither a raw form client nor guessed MFA inputs reproduce the browser's
JavaScript, challenge handling, and navigation lifecycle. The public page's
SSO script performs a domain SSO lookup; no new password-grant endpoint was
inferred from it.

The local `--browser` probe runs MyQ's pages in an isolated Chrome context with
the corrected unified parameters. Password/MFA inputs stay in the page. It
captures the final identity-origin HTTP redirect at Chrome's response stage,
before dispatch of `com.myqops://android`, and validates origin, state, PKCE,
expiry, and one-time exchange. It also handles a main-frame script navigation
to that URI. Callback URLs and browser storage are not printed or exported.
The HA browser handoff now reuses this browser transport with a public request;
HA retains the verifier and performs the exchange. See [browser-sign-in.md](../browser-sign-in.md).

An anonymous request subsequently returned **HTTP 429**, `Server: cloudflare`,
`Content-Type: text/html; charset=UTF-8`, and **`cf-mitigated: challenge`**,
without `Retry-After`. The page contained a browser-verification interstitial.
This is classified as browser verification, not a normal API quota response;
the raw transport reports `BrowserVerificationRequired`, and the interactive
browser remains open so its normal scripts and the user can complete the page.
The interactive browser helper neither solves CAPTCHA nor changes browser
identity/automation flags.
([Cloudflare's documented response marker](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/))

The optional `--curl` probe uses `curl_cffi.AsyncSession` with a single Android
Chrome profile and cookie jar for unified form login, token exchange/refresh,
and API reads. It preserves the library's profile headers instead of adding the
integration's User-Agent. Certificate verification, bounded body reads, manual
origin-checked auth redirects, and explicit challenge/rate-limit handling remain
in place. No JavaScript or App Check implementation is provided by this transport.
The default profile in curl_cffi 0.15.0 resolves to `chrome131_android`; this is
the library's available Android profile, not a fingerprint recovered from the
APK. Its anonymous authorization trial also returned a Cloudflare challenge.

The user supplied a successful website-login HAR, analyzed on November 16, 2026.
It contains 78 entries and identifies Chrome 157 on macOS. The observed sequence
on the identity service is:

| Request | Result |
| --- | --- |
| `GET /` | Public home page, 200 |
| `GET /Home/Secure` | Redirects through `/Account/Login` and `/Account/LoginSelect` |
| `GET /Account/LoginWithEmail?returnUrl=...` | Password form, 200; return URL is `/Home/Secure` |
| `POST /api/v1.0/externalaccounts/IsSSOEnabledForDomain` | JSON `email` input; boolean `false` response |
| `POST /Account/LoginWithEmail?returnUrl=...` | Redirect to `/AccountMfa/VerifyOtp?returnUrl=...` |
| `GET /AccountMfa/VerifyOtp?returnUrl=...` | Email OTP form, 200 |
| `POST /AccountMfa/VerifyOtp?returnUrl=...` | Redirect to `/Home/Secure` |
| `GET /Home/Secure` | Successful website landing page, 200 |

The captured SSO bundle performs its lookup on email focus-out; three such
requests appear in the capture. The HTTP implementation now checks once before
submitting a password when the page includes that identity-origin bundle. A
boolean `true` result requests browser SSO instead of posting the password.

The MFA POST contains `Otp`, `SelectedMfaMethod`, `IsSmsMethodEnabled`,
`MaskedEmail`, `MaskedPhoneNumber`, `__Invariant`, and
`__RequestVerificationToken`. `Otp` is a number input without `autocomplete`;
the captured bundle enables submission after six digits. The other fields are
hidden values taken from the current page. The existing parser correctly
recognizes this form, and a private comparison confirmed it reconstructs every
submitted field/value exactly for both the password and MFA forms. Tests use
the observed structure with entirely synthetic sensitive values. SMS, enrollment,
method switching, resend, and incorrect-code responses remain unobserved.

Form navigation uses same-origin fetch metadata; the SSO lookup uses AJAX/CORS
metadata. The curl adapter suppresses its document-navigation defaults for that
lookup. These details match the captured requests, but do not establish the
cause of any Cloudflare decision.

The HAR has no `/connect/authorize`, `/connect/token`, or `com.myqops://android`
callback. Cookie and Set-Cookie headers are absent from the export, so it cannot
establish the browser's initial cookie state or clearance mechanism. No captured
password, OTP, cookie, or token is replayed. A subsequent credential-free curl
check with `chrome146` reached the same website password form with HTTP 200;
the app authorization URL in that session still received a Cloudflare challenge
with HTTP 429. A website landing page is therefore never accepted as API tokens.

The `--browser-har` probe creates a fresh PKCE transaction and provides its
authorization URL for the user's normal browser. It reads a newly exported HAR
locally, accepts only an identity-origin HTTP redirect with matching state and
the exact registered callback, then exchanges the code once using its own verifier.
Old captures, ambiguous callbacks, provider errors, and expired sessions fail
explicitly. Captured passwords, cookies, and token response bodies are ignored.
The standalone probe owns its transaction. The separate HA browser helper
accepts an HA-owned public request and returns a private result file to setup.

A second HAR, `partner-identity.myq-cloud.com2.har`, contains 117 entries and
the successful app OAuth redirect. Its authorization parameters match the APK
and probe. The browser cleared its initial Cloudflare challenge, completed
sign-in, and received `code`, `state`, and `iss` at the registered app callback.
The callback request began about 32.7 seconds before the file's modification
time; that is an approximate export delay, not proof of authorization-code expiry.
The user reported successful callback validation followed by token HTTP 401.
That original probe discarded the rejection body, so its exact provider code
is unknown. The used authorization code has not been replayed.

Two anonymous diagnostic token requests using an explicitly invalid code and
no account credentials returned HTTP 401. The second inspected the provider
fields: `code=401.122`, `message=Unauthorized`, and description
"The application is unauthorized." This establishes an application rejection
for those diagnostic requests; it does not recover the original failed exchange's
body. `ip0/j.java` handles `401.122` separately from invalid-grant/token errors,
and `ft0/o0.java` and `hp0/c.java` preserve that distinction.

`ip0/k.java` and `ip0/s.java` show authorization-code exchange using `client_id`,
`scope`, `grant_type=authorization_code`, `code`, `redirect_uri`, and
`code_verifier`. `ip0/n.java:r()` asks the app's attestation provider for a token
before exchange. `ml0/f.java` connects that provider to Firebase App Check and
Play Integrity. `ip0/k.java:E()` attaches `Firebase-AppCheck-Token` when acquired
and has separate branches for provider failure or an unconfigured SDK. This
implementation does not produce an Android attestation. Runtime enforcement
cannot be determined from the APK or a browser challenge on the initial GET.

The token acquisition chain is also present: `hu/a.java` registers the Play
Integrity App Check provider (`hr1/b.java`); `ml0/f.java` requests a cached or
fresh Firebase token; `ir1/i.java` obtains a Firebase challenge, passes its nonce
and project number to Google's `IntegrityManager.requestIntegrityToken`, and
exchanges that signed integrity result through Firebase; `er1/o.java` contains
the `exchangePlayIntegrityToken` URL. MyQ then supplies the resulting token to
`ip0/k.java` for the header above. This is distinct from browser cookies and
Cloudflare clearance. The APK's public configuration alone is insufficient to
produce a signed integrity result.

Neither supplied HAR contains an App Check token header or an App Check service
request. The second loads Firebase Analytics/Installations; its generic Firebase
base bundle mentions App Check component names but does not initialize it.
The MyQ-specific `myqFirebase` bundle calls Analytics only. A different web
application could implement web App Check, but that is not observed here.

The probe's `--app-check` option accepts a genuine supplied token at a hidden
terminal prompt. It is passed only to authorization-code exchange, never saved
in OAuth tokens/config entries, and not reused for refresh or device requests.
The new supplied/file providers check JWT expiry as an unverified local freshness
hint. MyQ still validates authenticity and expiry. They support bounded
acquisition, serialized caching and invalidation, but no native token producer
or automatic native attestation renewal exists yet. Rejection reports
now retain only allowlisted OAuth/MyQ codes and safe metadata; `401.122` has a
distinct application-verification error. No placeholder or fabricated App Check
token is sent.

`ft0/t0.java` shows refresh with `client_id`, `grant_type=refresh_token`,
`refresh_token`, and the `IsRefresh: true` header. `ap0/AuthLoginDto.java`
defines the access token, refresh token, expiry, and token type. `ft0/i0.java`
attaches Bearer authentication to service requests. The implementation renews
60 seconds before expiry, serializes concurrent refreshes, saves rotated
refresh tokens, and retries a rejected GET at most once. Mutations are not
replayed, including after a 401.

The native request metadata recovered in the [authentication review](auth-review.md)
is now included on token exchanges and the supported MyQ account/device hosts:
`BrandId: 1` and `App-Version: 5.324.0.1132442`. These headers are not added to
browser navigation or camera-service requests. Their effect on live authorization
has not been established.

The implemented account/device contracts are:

| Method and path | Service | Response/request facts | Evidence |
| --- | --- | --- | --- |
| `GET /api/v6.0/my/profile` | profiles | `user_id`, `email` | `b91/d.java`, `a91/t.java`, `c91/f.java` |
| `GET /api/v6.0/accounts` | accounts | object containing `accounts`; each has `id`, `name` | `an0/b.java`, `zm0/f.java`, `bn0/Account.java`, `bn0/AccountsListDto.java` |
| `GET /api/v6.0/Accounts/{accountId}/Devices` | devices | `items`, `count`, `href` | `gz0/a.java`, `hz0/b.java`, `yc1/DeviceList.java` |
| `PUT /api/v1.0/accounts/{accountId}/devices/{serial}` | devices-action | `action`: `open`/`close`; `trigger`: `myq_an`; `device_family`: `garagedoor` | `bz0/e.java`, `bz0/d.java`, `az0/DeviceActionRequestBody.java`, `zy0/a.java` |
| `PUT /api/v6.0/accounts/{accountId}/lamps/{serial}/on` or `/off` | account-devices-lamp | no body | `p01/a.java`, `o01/e.java` |

`wy0/i.java` selects device API v6.0/v6.1/v6.2 using app feature flags. This
implementation uses the observed v6.0 path; the server behavior for the user's
account has not been confirmed. It reports a partial-list error if `count`
exceeds the returned items, rather than silently pretending discovery is
complete. No pagination mechanism is established by `DeviceList`.

`yc1/f.java` and `yc1/DeviceAttributes.java` define device identity, family,
type, capabilities, and state fields. Door commands require a known door type,
the `garagedoor` family, online status (missing reads as true, `k.p1`), and
none of the app's precheck blocks (`places/view/b.java:C`): monitor-only, PIN
lockout, work-light error, or a virtual opener's learned door sensor with a
fault. A missing unattended-action flag reads as allowed (`k.A1`; the app never
reads the open flag); an explicit false denies that direction. Open/closed/opening/closing/stopped are reported from the
server; ambiguous movement stays unknown. The implementation does not assume
a stop or percentage-position command.

**Garage-door control verification — November 20, 2026**

The base APK SHA-256 above was re-established, and the APK inventory recorded
its 6,344 ZIP entries (6,345 inventory records including the artifact root).
The code trace reuses the separately generated JADX 1.5.7 sources from the
authentication investigation, with their original exit status 3 and 558
reported errors. These are distinct evidence sources:
the inventory establishes artifact identity; the JADX trace establishes the
following static relationships. Private evidence and reviewed-file hashes are
retained in a private checkpoint (not in the repository).

| Question | Static evidence and integration result |
| --- | --- |
| Which actions? | `a01/b.java:H2` and `c2` construct `az0/c.java:GDOAction` for `close` and `open`. That action enum has no stop or percentage-position member. HA advertises only open/close, subject to permissions. |
| Which request? | `zy0/a.java:a` builds `DeviceActionRequestBody`; `bz0/d.java:g,f` selects `v1.0`; `bz0/e.java:a` declares the PUT route above. The integration matches the action, `myq_an` trigger, and `garagedoor` family. |
| Which host and authentication? | `cv0/o.java` binds the action API to `oy0/b.java:DEVICES_ACTION`; the production config supplies `.myq-cloud.com`. `ft0/i0.java` adds OAuth authorization and common native headers. The integration uses Bearer authorization plus the observed BrandId/App-Version. The reviewed command path does not acquire App Check separately. |
| How is state interpreted? | `devices/u.java:e,l0` lowercases `door_state`, then parses the `model/m.java` state enum. Known states are now normalized in the integration; malformed or unrecognized values remain unknown. `moving` has no known direction and is not guessed as opening/closing. The enum decompilation has restoration warnings. |
| How are permissions handled? | `yc1/DeviceAttributes.java` declares online, monitor-only, and unattended-action fields. The integration follows the app's defaults: missing `online` and unattended flags read as true (`k.p1`, `k.A1`), missing monitor-only reads as false (true for virtual openers). Explicit false or malformed values disable the control. The app's precheck blocks (PIN lockout, work-light error, faulty learned sensor on a virtual opener) also apply. |
| Does an accepted request mean the door moved? | No. The SDK action returns a unit/result status; the integration reports the returned state without assuming movement. Like the app, it then tracks the device until it reports the commanded state, within the app's per-type window; it re-reads only that account, every 5 seconds rather than the app's 1–2 seconds. Afterwards it uses its configured poll interval (60 seconds by default). See [native follow-up polling](../api/devices.md#state-confirmation-roles-and-diagnostics). |

The comparison found and corrected two input-handling defects: list/dict door
states could raise a TypeError, and malformed monitor-only values could leave
controls enabled. State normalization also now follows the inspected app's
case-insensitive parsing. Regressions exercise the actual HA cover service
path with synthetic HTTP responses, both wire actions, state decoding,
permission changes, and error handling.

The transport review also reproduced an implicit retry in aiohttp 3.13.3:
a local server consumed a PUT, disconnected before acknowledging it, and
received the PUT again. Version 0.1.6 adds a request-scoped send guard for
mutations, including commands. It permits only one send-handler invocation,
even if aiohttp or an outer middleware retries. Existing session middleware
is preserved, and the shared HA session's configuration/lifetime is unchanged.
This requires aiohttp 3.12+; preserving its middleware currently uses the
private `_middlewares` attribute because no public getter exists. If that
interface is unavailable, mutations fail before sending. A real loopback
regression verifies one received PUT, an uncertain command result, preservation
of session middleware, and unchanged GET behavior on the same shared session.
The guard does not claim exactly-once execution by the remote service.
See [aiohttp's middleware retry behavior](https://docs.aiohttp.org/en/stable/client_advanced.html#middleware-chaining)
and [per-request middleware](https://docs.aiohttp.org/en/v3.13.3/client_reference.html#aiohttp.ClientSession.request).

The integration does not automatically resend a PUT after a 401, timeout, or
server failure. A per-device lock excludes overlapping requests only while
the first request is in flight; it is not a lock until physical movement
finishes. A successful HTTP response is not proof that the requested end state
was reached. Stop/position control, opener obstruction behavior, device-specific
capabilities, server permission enforcement, and physical operation remain
unverified. This review covers APK 5.324, not command equivalence with the
installed Play Store 5.325 build. No live account/device request or garage-door
operation was performed; authentication remains the blocker to live validation.

Camera contracts are separate from the account/device service:

| Method and path | Use | Evidence |
| --- | --- | --- |
| `GET /cxs/api/devices/{id}` | Details including `settings`, `cap`, `status`, `hw`, `net` | `f41/a.java`, `j41/a.java`, `g41/TendDeviceInfo.java` |
| `GET /cxs/api/devices/{id}/cam/recentImage.jpg?size=FHD&forceUseExisting=true` | Cached recent JPEG | `hj0/m.java`, `com/chamberlain/myq/features/places/view/e.java` |
| `PUT /cxs/api/devices/{id}/settings` | Partial settings updates | `f41/a.java`, `g41/TendSettings.java`, `h41/` update DTOs |

These calls use Bearer authentication and `AntiCSRF: AntiCSRF`, as shown by
the shared client setup and `ot0/a.java`. `je1/a.java` strips the serial prefix
through the final hyphen for the Tend device ID. No camera association is
invented when the API returns only an opener reference without a camera record.

Implemented camera fields are `settings.cam.enabled` gated by
`cap.cam.enableSwitch`, `settings.cam.motionEnabled` gated by `cap.cam.motion`,
`settings.mic.enabled` when microphone capabilities/settings exist, and
`settings.spk.vol` within `cap.spk.volMin`/`volMax`. Their DTO evidence is in
`g41/Cam.java`, `g41/CamCapabilitiesDto.java`, `g41/Mic.java`, `g41/Spk.java`,
and `g41/TendSettings.java`. The server's partial-update behavior remains to
be validated with an authenticated camera session. JPEG reads are cached
locally for 30 seconds and do not imply current live video.

Live video evidence includes `f81/b.java`'s required-video handshake with
connection type 12, an accept response whose server options may contain `kvs`,
`j81/KvsModel.java` fields for channel URL/ARN/name, endpoints, ICE servers,
and region, and `h81/KvsCredsPushDto.java` for pushed credentials. The bundled
`VideoConnectionManager`/`SscpRtpConnection` also use SDK/native signaling.
The recovered mode-12 handshake and HA media transport are implemented in 0.1.7
as described below. Legacy SDK/native RTP transport remains unimplemented.

### Camera investigation — November 20, 2026 (0.1.7)

This pass reused the exact 5.324 APK and artifact inventory from the door
investigation. Code tracing used the existing JADX 1.5.7 output, plus targeted
`--show-bad-code --comments-level debug`
decompilation of the factory, handshake coordinator and WebSocket client.
Coroutine reconstruction warnings remain. These are static observations; no
running camera was observed and the decompiled code was not executed.

| Boundary | Observed contract | Source in JADX output |
| --- | --- | --- |
| Camera discovery | Case-insensitive native camera types, including newer `SmartGarageLCCam2`/regional variants | device model `o.java`; `DeviceDeserializer.java` |
| Fresh snapshot | Bearer + AntiCSRF; `GET /cxs/api/devices/{id}/cam/recentImage.jpg?size=HD&forceRefresh=true` | `wy/o.e`, `hj0/m.z,g`, `xr0/v` |
| Capture time | Response header `tend-dttm`; not a guarantee that a refresh produced a new frame | `hj0/c0.intercept` |
| Two identifiers | HTTP uses device `id`; video signaling uses separate `alias` | `TendDevice.getDeviceId/getDeviceAlias`, `g41/TendDeviceInfo` |
| Stream selection | `cap.cam.kvsWebRTC`; Android also checks feature flag `enable_mode12_live_streaming` | `f81/g.a,b`, `ag1/a.h`, `CamCapabilities` |
| Wake-up | `POST /cxs/api/devices/{id}/do/wakeup`, JSON `{}`, before video for sleeping/battery models | `DevicesManager.wakeup`, `TendDevice.checkRequiresWakeup`, `HttpMyqCoreRequestManagerImpl`, `wf1/f.e`, `vf1/g.a` |
| Tend connection | Raw TLS to `server.seedonk.com:5104`, not an HTTP servlet request | `ConnectionManager.setComm`, `TendAuthHost`, `eg1/b`, `cg1/a` |
| Tend authentication | Cxnet action 519; empty username and `myQ.accessToken:` + OAuth access token | `SeedonkAuthManager.attemptLogin/getTendSessionId`, `ConnectionManager.login` |
| Cxnet framing | 27-byte little-endian header, session/routing fields, bounded options/data | `fg1/h`, `fg1/f`, `eg1/b` |
| Start | Action 411 to camera alias; `require-video-send|{userAlias}|12|` | `f81/c`, `f81/d`, `ConnectionManager.sendCxnetCommand` |
| Acceptance | Matching camera alias/mode, five-second wait; action 438 or batched 501 delivers `serverOpt.kvs` | `f81/b.t,x`, `eg1/b`, `CommandHandler` |
| KVS credentials | Signed `channelUrl`, region, ICE URLs/username/password/optional TTL | `j81/KvsModel`, `j81/IceServerModel` |
| WebRTC | H.264 video, PCMU/PCMA audio; SDP offer and ICE messages over the supplied WSS URL | `j81/d`, `KvsSignallingService`, `WebSocketMessage`, `KvsEventParser` |
| Signaling encoding | Outgoing Base64url JSON with padding; `action`, `messagePayload`, `senderClientId`, empty `recipientClientId`; inbound `messageType` | `WebSocketMessage`, `KvsEventParser` |
| Stop/keepalive | `stop-video-receive|{userAlias}|12|`, logout action 408; keepalive action 526 every 35 seconds | `f81/b0.j`, `f81/c`, `ConnectionManager`, `eg1/b` |

The implementation uses the device's explicit capability rather than the
Android application's remotely configured feature rollout. Wake-up selection
matches positive `status.pwr.sleepInterval` or the eight native battery platform
models. Android waits for SDK status pushes with a 30-second fallback read; HA
instead polls details at most once a second for up to 30 seconds. A failed wake
aborts viewing. A wake command or native start is never automatically replayed.
The implementation follows only bounded, verified-TLS redirects within Tend's
service domains and rejects unknown destinations.

HA's frontend configuration callback is synchronous and cannot fetch per-viewer
TURN credentials during setup. `api/live.py` therefore creates two aiortc peers:
the receiving camera peer uses the returned ICE servers and sends native-style
SDP/ICE signaling; its tracks feed a browser-facing, send-only peer. No browser
microphone track is sent to the camera. A loopback test transports a real
generated H.264 frame through this bridge. The dependency is pinned to aiortc
1.16.0. Home Assistant pins PyAV for its stream integration (16.0.1 in 2026.2,
17.0.1 from 2026.7, 19.0.0 from 2026.10), and aiortc 1.16.0 accepts PyAV 14 to
19. aiortc 1.14.0 accepted only up to 16, so HA 2026.7 and later could not
install it and the config flow failed with a 500 error.

Views are limited to one viewer per camera and ten minutes, with explicit stop
and cleanup on cancellation, error, off/offline/removal and integration unload.
These limits are implementation choices. No automatic reconnect, legacy RTP,
two-way audio, recording or cloud-history playback is implemented. The app's
`KvsCredsPush` route updates the warm/idle signaling cache; its expiry sweep skips
busy channels. This trace did not establish that an active view must restart at
the signed URL's connection expiry, so that expiry is checked when connecting,
not treated as a live-media deadline. The integration does not prewarm channels.

The standalone `video.m3u8` path found in `n71/h` belongs to recorded-event
preloading. The separate `webrtc-app/api/v1/videoCalls/{callId}/metadata` interface
belongs to video-call handling; neither establishes a garage-camera live URL.

Evidence/provenance are preserved in a private checkpoint (not in the
repository). Live validation remains an open question. The questions about
native snapshot transport and the mode-12 signaling path are answered
statically and tested locally. Target-camera
capabilities, account/subscription authorization, real image freshness,
long-running media behavior and Android 5.325 equivalence remain unresolved.
No authenticated MyQ request, image retrieval or physical command was sent.

[api_inventory.json](../api/api_inventory.json) contains **464 static declarations**:
363 ordinary Retrofit annotations, 9 generic Retrofit HTTP annotations, and
92 Tend/Seedonk SDK table entries. Duplicates reflect different methods/DTOs
or call sites. Literal paths retain their original prefixes; base URLs require
separate resolution. The inventory may include bundled-library routes not used
by MyQ. It omits unresolved runtime/native behavior and is not a list of
implemented features or proof of server authorization.

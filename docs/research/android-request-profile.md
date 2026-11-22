# Android authentication request profile

November 21, 2026. This pass re-read the existing JADX CLI 1.5.7 export of MyQ
Android **5.324.0.1132442** (`com.chamberlain.android.liftmaster.myq`). It did not
repeat decompilation or perform a live MyQ sign-in. Isolated runtime probes and
a Firebase debug-token exchange were subsequently completed as described below. The original export exited 3
with 558 errors; readable methods support the bounded findings below. The base
APK SHA-256 is
`20d6e5d35a819169867f1b7ebbe13769720ae0b78cb6e26ef95bc9f36dabefb2`.

## Verified request details

Source paths are relative to the private export's `sources/` directory.

| Detail | Static source | Implementation |
| --- | --- | --- |
| Native identification | `y92/b.java:232–237` builds `Build.MODEL + "/Android " + Build.VERSION.RELEASE`, `BrandId` and `App-Version`; `ft0/i0.java:129–137` applies the map | Android-format User-Agent plus existing brand and analyzed app version |
| Transport defaults | `okhttp3/internal/http/BridgeInterceptor.java:29–75` adds gzip and HTTP/1 Keep-Alive, with no default `Accept` | Gzip only; HTTP/1 Keep-Alive; suppress automatic Accept and browser-only headers |
| Browser/native cookie separation | `com/chamberlain/myq/sdk/core/network/a.java:192–216,321–324` creates the native client without a cookie-jar override; `okhttp3/OkHttpClient.java:594` and `okhttp3/CookieJar.java:13,24–35` select NoCookies | Native requests exclude browser cookies; HA uses a separate curl native session with the measured Android profile |
| Form serialization | `okhttp3/FormBody.java:35–39` and `okhttp3/internal/url/_UrlKt.java:118–158` | Code exchange, refresh and revocation use `+` for spaces, `%2B` for literal plus, `%7E` for tilde and literal `*` |
| OAuth fields and proof | `ip0/k.java:44–72`, `ip0/s.java:68–78`, `ft0/t0.java:30–48` | Existing Android client, scope, redirect, PKCE and optional App Check header retained; no client-secret field found in this builder |

## Runtime identity and transport

The selected native User-Agent is **`sdk_gphone64_arm64/Android 17`**, using
actual `Build.MODEL` and `Build.VERSION.RELEASE` from the saved emulator. Its
AVD display name, Pixel 9 Pro XL, was previously mistaken for its model. The
app version remains tied to the analyzed 5.324 APK.

An isolated helper loaded the original APK's OkHttp classes and `ft0.p0` socket
wrapper on Android 17, then contacted only a local TLS recorder. It negotiated
TLS 1.3 and HTTP/2. The measured profile is implemented in `api/android_tls.py`
and used by `Client` through `AndroidHttp` in Home Assistant, including initial
exchange, refresh, revocation and native account/device requests. Hosted pages
and other services keep separate aiohttp sessions. Native connections are pooled
and closed on flow disposal, unload, setup failure and shutdown.

- JA3 hash: `1d714db2228763eab228fc28ce7f8e4f`.
- HTTP/2 profile: `4:16777216|16711681|0|m,p,a,s`.
- No GREASE or PRIORITY frame; ALPN offers `h2`, then `http/1.1`.
- A curl_cffi 0.15 replay matched the complete captured ClientHello after
  normalizing only its random bytes, session ID and X25519 key material. Cipher
  order, extension contents/order, groups, signature algorithms, HTTP/2 settings,
  window increment, pseudoheader order and ordinary GET header order matched.
  A final capture through the repository's actual `CurlHttp.request` path
  reproduced those results, including engine-managed padding and HTTPS HTTP/2.
- The replayed OkHttp request used stream 3; curl used stream 1. SETTINGS ACK
  timing also differed. HTTP/1 fallback, POST framing, TLS resumption and pooled
  connection behavior were not compared with Android. This is a controlled
  client-construction replay, not an original-app or physical-phone capture.

The hidden email/password path creates a default Android WebView, enables
JavaScript and calls `loadUrl` without a UA override (`ip0/j.java:865–866,
1000–1002,1153–1154,1517–1543`). That legacy APK path uses `unified=False`.
Separate HTTP and HTTPS helper captures measured WebView **158.0.8127.46**:

```text
Mozilla/5.0 (Linux; Android 17; sdk_gphone64_arm64 Build/CE2A.260420.051.A2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/158.0.8127.46 Mobile Safari/537.36
```

Those measurements describe the legacy WebView path and remain valid evidence
for that runtime. The integration's hosted profile has since changed after the
working-reference comparison below. It now uses the unified authorization flow
and a mobile Chrome 153 identity, including the reduced Android browser UA,
client hints, navigation Accept, and origin/referrer/form-content-type on POST.
Native device metadata remains separate.

## Working-reference comparison

On November 21, the user supplied `../ha-myq.sav-ui` as working source to inspect,
with an explicit instruction not to copy it. It was read without modification or
execution. The review covered its auth, form/page parsing, OAuth, App Check and
config-flow behavior. Implementations and test fixtures in this checkout were
written independently by extending its existing `FormParser`, `Login`, OAuth
transaction and Firebase provider.

| Behavior seen in the reference | Gap in this checkout | Current behavior |
| --- | --- | --- |
| Unified authorization | Password login selected the legacy flag | Unified is now the default; the older research option remains explicit |
| Mobile Chrome hosted identity | Hosted headers identified an Android WebView | Navigation uses the supplied Chrome 153 UA/client hints and header set |
| Direct credential submission | A website-script focus-out lookup added an extra SSO request | Unified login posts credentials directly; the captured legacy SSO helper remains available |
| Field metadata determines credential and OTP controls | Fixed credential names and a small OTP-name list rejected variants | Unambiguous metadata matching and a bounded fallback on the known verification endpoint |
| Email verification selection | Existing SMS selection was accepted without a preference | Known SMS verification page is switched to email once, unless email is explicitly disabled |
| Checked form controls | Checkboxes were omitted | Successful controls are retained, including repeated names and their order |
| Consent after MFA | A consent form was classified as an unsupported screen | The known form is submitted once with its affirmative action |
| Consent can acknowledge with HTTP 200 | No authorization continuation after that acknowledgement | Resume the validated identity-service authorization callback |
| Validation classes | Only `id=validation-errors` was recognized | Summary and field error classes are also recognized; server text stays private |
| Firebase debug exchange before OAuth | Already implemented in the previous pass | Retained with private configuration, caching, app/audience and expiry checks |

The working reference uses ordinary aiohttp for hosted pages and native token
requests. It does not implement Android TLS fingerprint emulation. This corrects
the earlier emphasis: Python's hosted-page TLS is a difference from Android,
but this working reference does not establish it as an obstacle to login.
Functional flow gaps are independently testable without exact TLS parity.

Its older native app-version/device labels and different refresh metadata were
not adopted: this checkout retains the values and refresh behavior evidenced by
the inspected APK. It also retains strict callback/state validation, single-use
code handling, origin restrictions and session/account validation. Browser
fallback menus from the reference remain excluded as requested. The private
registration file remains separate from distributed code.

Fresh tests cover password/MFA, an invalid-code retry with updated CSRF values,
consent redirect and HTTP-200 responses, email method switching, repeated checked
fields, unsafe continuation rejection, and lost-consent-response replay guards.
A Home Assistant flow test completes credentials → MFA → consent → Firebase →
OAuth → entry creation with synthetic responses. The user reports that the
reference works; no live account sign-in was executed during this comparison.

## Firebase exchange

`api/firebase.py` independently implements Firebase's documented
`exchangeDebugToken` request. It reads private registration from
`myq_app_check.json`, sends the package and uppercase signing-certificate SHA1
to Firebase, and caches the returned App Check proof until its remaining
validity falls below five minutes. It validates the response's app/audience and
bounds expiry by both JWT `exp` and Firebase `ttl`. Local claim checks do not
verify a signature. Google's HTTPS response supplies the token.

The runtime's `http.agent` property supplies the Firebase request UA:

```text
Dalvik/2.1.0 (Linux; U; Android 17; sdk_gphone64_arm64 Build/CE2A.260420.051.A2)
```

The supplied registered debug credential was accepted by Firebase on November
21, 2026, returning a fresh proof for its expected app and project. That
registration targets a different project from the APK's production resources.
Neither credential nor proof is recorded in source, diagnostics, HA entries or
release archives. This is a debug-provider exchange; it does not reproduce the
production app's Play Integrity attestation. Firebase documents both the
[registered-debug exchange](https://firebase.google.com/docs/reference/appcheck/rest/v1/projects.apps/exchangeDebugToken)
and [its registration requirements](https://firebase.google.com/docs/app-check/android/debug-provider).

MyQ acceptance of that proof with a fresh user authorization code remains
unverified. No password/MFA attempt, account read or physical-device command
was made during the runtime probes or Firebase verification. The next required
check is a successful MyQ exchange followed by profile, discovery and refresh.

Private runtime evidence was saved under `/private/tmp/ha-myq-native-profile-20261121/`
and `/private/tmp/ha-myq-webview-profile-20261121/`. Both helper apps were uninstalled,
local servers stopped and the read-only emulator stopped after the probes.

## Reused source identities

These hashes identify the reviewed decompiler output, not original source or
runtime captures. Raw APKs, decompiled code and private captures stay outside
the repository.

| Decompiled file | SHA-256 |
| --- | --- |
| `y92/b.java` | `dcd49e526710802ac6f221ea7d75dc1ee5427adf34e96368ba7acd658812d367` |
| `ft0/i0.java` | `c3fd719b1dd4aeba9c33c0780d1d061b392a55d7e05c59c560dcccc762f57764` |
| `ip0/k.java` | `83fbc39e997f097c30fa4a2bfb8c0393516177dbf2d98ed92cc13aef9408d8a3` |
| `ip0/s.java` | `af5d65134a5ee2df21003c3e6119968006dc6f155157d18139db73a961d940b2` |
| `okhttp3/FormBody.java` | `bf6774978b512ba87bc0adde9ad43434a17cb0d498958369897b08c1db2e1711` |
| `okhttp3/internal/url/_UrlKt.java` | `b64b1b5a52113e37862b6ac134ee87aa51c1c5d0a49bf36ed8c8c24d072bb909` |
| `okhttp3/internal/http/BridgeInterceptor.java` | `3627cde82d62265ca89490e7f38971847c64f4f3f3e20cdb608cd657e5da11b2` |
| `com/chamberlain/myq/sdk/core/network/a.java` | `1ca52c30d015f233134748bd30290bdd7910e29ca3abe75e925d55ce196a5bda` |

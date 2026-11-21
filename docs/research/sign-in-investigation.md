# Sign-in investigation

This is the record of attempts to obtain a MyQ API session, kept separate from
the [authentication reference](../api/authentication.md) that describes the
protocol. Dates are 2026.

These attempts used earlier versions of the sign-in code. The sign-in code has
been reworked since, and the current version has not been tried against MyQ.

## Summary

- **Hosted sign-in works.** In a browser, MyQ's password page, email
  verification code and the OAuth callback to `com.myqops://android` all
  succeeded, with the expected Android parameters, state and issuer.
- **The token exchange does not.** Exchanging a real callback code returned
  HTTP 401; its response body was not kept, so the exact reason is unknown and
  the code was not replayed. Separate exchanges with deliberately invalid codes
  returned `401.122`, "The application is unauthorized".
- **App Check is a suspected blocker.** The Android app adds a Firebase App Check
  token from Google Play Integrity to that exchange. No genuine token has been
  available here, and the browser sign-in pages never request one: their Firebase
  code only initializes Analytics and Installations. Missing App Check is a
  confirmed difference and a plausible cause, not a proven sole cause; the app's
  `BrandId` and `App-Version` headers were also missing at the time.
- **Native request details updated November 21.** The integration now uses a
  selected Android device-format User-Agent, native gzip/HTTP/1 connection
  headers, separate browser/native cookie handling and OkHttp form escaping.
  These changes come from static inspection; no accepted exchange or matching
  TLS/HTTP/2 fingerprint has been demonstrated. See the
  [request profile evidence](android-request-profile.md).
- **Other routes tried:** the iOS app's client (`IOS_CGI_MYQ`, `com.myqops://ios`)
  also returns `401.122`. On an Android emulator, the real app's own sign-in
  fails with `401.122 (-998)`; the device was reported uncertified, with no Play
  Integrity labels. Browser TLS fingerprints (`curl_cffi`) still hit MyQ's
  Cloudflare challenge.
- **One-time sign-in link:** never tried, because no link was found (see the
  [authentication reference](../api/authentication.md#refresh-persistence-and-sign-out-surfaces)).
  In November its page, `https://www.myliftmaster.com/ids/auth/onetime-login`,
  began answering that web login is no longer supported and to use the mobile
  app, so the option was removed from setup.

## Browser verification

Anonymous requests to the authorization page returned HTTP 429 with HTML and
`cf-mitigated: challenge`, without `Retry-After`. That marks a Cloudflare
browser check, not an ordinary rate limit, and it is a different stage from App
Check. An interactive browser passes it; a raw HTTP client does not.

The `curl_cffi` trial on November 15 (version 0.15.0, profile
`chrome131_android`) received the challenge before the password page, as did
earlier default and desktop Chrome profiles. Choosing a TLS or HTTP profile is
therefore not a demonstrated way past it. The exported HARs omit some cookie
headers, so they cannot reconstruct a successful browser's clearance state.

## Observations and what they establish

| Observation | Supported conclusion | Remaining limit |
| --- | --- | --- |
| Website HAR, analyzed November 16: 78 entries, successful password and email OTP, `/Home/Secure` 200 | Hosted website login/MFA succeeded | No app authorization/code/token request in that HAR |
| App authorization HAR: 117 entries, expected Android parameters, code/state/issuer callback | Browser authorization and callback binding succeeded | Python's later token request is outside the HAR |
| Real callback exchange returned HTTP 401 | That exchange failed | Original response body was discarded; its precise provider error is unknown; code was not replayed |
| Separate invalid-code anonymous diagnostics returned HTTP 401 / `401.122` | Those diagnostic requests were rejected as unauthorized applications | Does not recover the original response or prove missing App Check was its sole cause |
| Browser HARs had Analytics/Installations but no App Check exchange | No usable proof source in those captures | Does not prove every MyQ web surface lacks App Check |
| Scoped iPhone capture observed `IOS_CGI_MYQ`, `com.myqops://ios`, same scope and S256 | iOS authorization parameters independently observed | No iOS token exchange or attestation captured; iOS binary not analyzed |
| iPhone identity TLS included `certificate_unknown` after a local HTTPS trust check succeeded | App-specific trust restriction is plausible | Pinning implementation and iOS attestation mechanism remain unknown |
| Invalid-code iOS-client diagnostic also returned `401.122` | Changing client/redirect alone did not create a working session | Does not identify the exact iOS proof/header requirements |
| Emulator normal login showed `401.122 (-998)` before proxy setup | Native login failed independently of this interception setup | Analyzed mapping is from 5.324, emulator app is 5.325.0.1132545 |
| Emulator: Android 17/API 37 ARM64 Google Play image, Pixel 9 Pro XL, Play Store install, no global proxy | Identifies the observed environment | Hardware profile selection is not itself an integrity guarantee |
| User reported uncertified device and Play Store `Labels: []` | Separate diagnostic returned no integrity labels; supports an integrity-related blocker | Not MyQ's decoded verdict, not its exact Firebase policy, not proof all emulators fail |

The temporary iPhone proxy/certificate setup was removed and normal native app
access restored. No token file was acquired. No emulator model/version change
has been demonstrated to solve this blocker. This project has no genuine
supplied App Check token.

The November 21 request profile uses `Pixel 9 Pro XL/Android 17`, taking the
model and OS from the earlier emulator record. It is an explicit compatibility
choice, not a claim that the integration runs on that device or that this
profile passes Play Integrity. The user has no working Android phone available
for a successful native capture. No new live sign-in was performed for the
request-profile change, and genuine proof acquisition remains unresolved.

## Artifacts

| Source | Identity / scope |
| --- | --- |
| Android package | `com.chamberlain.android.liftmaster.myq`, version `5.324.0.1132442` |
| XAPK SHA-256 | `77aee562ebbef96712ac44b41ee899047f3a1e92860fe6e32d760e5422c7d7f5` |
| Base APK SHA-256 | `20d6e5d35a819169867f1b7ebbe13769720ae0b78cb6e26ef95bc9f36dabefb2` |
| Java source analysis | Supplemental local JADX CLI 1.5.7 export, kept outside the repository; exit 3, 558 reported decompilation errors |

The APK signing identity has not been compared with a trusted Play Store copy.
The emulator ran a newer build (5.325.0.1132545), which was not analysed.
Method traces come from the JADX export (558 reported decompilation errors). The
artifact inventory was recorded separately.

## Related records

- [Authentication review, November 20](auth-review.md): the gap analysis that
  led to the current sign-in implementation.
- [Android request profile, November 21](android-request-profile.md): native
  identification, cookie boundaries, form encoding and transport limits.
- [Protocol evidence](protocol-evidence.md): capture provenance and the
  door-control and camera investigations.
- [iPhone capture helper](native-capture.md) and [account probe](probe.md): the
  tools used.

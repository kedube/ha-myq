# Sign-in

**Connect MyQ** opens directly to email and password. This is the only sign-in
method in Home Assistant, including when reconnecting an existing account.

1. Open **Settings → Devices & services → Add integration → MyQ**.
2. Enter your MyQ account email and password.
3. Complete the verification prompt if MyQ asks for a method or code.

Setup prefers email when the verification page offers that method. It handles
the known application-consent step automatically and continues to the token
exchange; a consent acknowledgement page alone is not a completed sign-in.

Your password and verification code are used for this sign-in only and are not
saved. There is no sign-in menu, browser-helper handoff, refresh-token import,
or manual app-verification token field.

The private install ZIP includes the registration needed for automatic app
verification. Extract `myq-install-private.zip` into Home Assistant's configuration
directory (usually `/config/`), preserving the folders in the archive, and restart
Home Assistant. It installs both `custom_components/myq/` and
`myq_app_check.json`; there is no separate JSON-file installation step. Keep this
ZIP private because it contains the reusable registration credential.

Public release ZIPs contain the integration code only. If installing one of those,
provide `myq_app_check.json` beside `configuration.yaml` separately. The integration
exchanges that registered credential with Firebase automatically; there is no
token-paste step. See [registration configuration](#registration-configuration).

The registration remains outside the component folder so integration updates
preserve it. It is read on each sign-in attempt, before credentials are sent;
updating just that file does not require a restart.

Firebase proof acquisition has been verified with the supplied registration.
End-to-end MyQ sign-in remains unverified; a hosted browser challenge or MyQ's
rejection of that proof can still stop it.

## Finishing setup

After sign-in, Home Assistant checks your MyQ profile and finds your
devices before it saves the integration. If that check fails, for example
because of a network error, a **Finish connecting MyQ** step lets you retry with
the session you already have. You don't need to sign in again.

Each MyQ account can be added once. The sign-in page is shown in Home
Assistant's language and country.

## Staying signed in

- **Renewal:** the access token is renewed shortly before it expires, on the
  app's schedule. Renewed tokens are saved straight away.
- **When you are asked to sign in again:** only when MyQ ends the session,
  which is when the token service answers `invalid_grant`, `401.102` or `400`.
  The app signs out in the same cases. Home Assistant then shows a
  **Reconnect MyQ** notice. You must sign in to the same MyQ account.
- **Other renewal errors** keep the session. Devices are unavailable until a
  later poll renews successfully.
- **A rejected request** (HTTP 401 from one MyQ service) is renewed and sent
  once more if the app would do so. If it is still rejected, only that request
  fails.

## Removing the integration

Deleting the integration cancels its MyQ session, as the app's sign-out does.
This is best effort and waits at most 15 seconds. It ends only Home Assistant's
own session; the phone app and other sign-ins stay signed in.

## Error messages

| Message | Meaning |
| --- | --- |
| Place myq_app_check.json in your Home Assistant configuration directory | The private registration file is missing. Copy it beside `configuration.yaml` and try again; it is excluded from the release ZIP |
| Home Assistant cannot read myq_app_check.json | Check the file's permissions and that the Home Assistant process can read it |
| myq_app_check.json is invalid | Restore the complete private registration JSON with all required fields; do not substitute a short-lived App Check token |
| Could not obtain app verification from Firebase | The registration file was read, but proof acquisition failed. Check Home Assistant's connection to `firebaseappcheck.googleapis.com` and try again |
| MyQ rejected the credentials or session | Wrong password or code, an expired sign-in request, or a refresh token MyQ no longer accepts |
| Verification was not completed | The verification code was not accepted; enter it again |
| MyQ rejected verification of the application (401.122) | MyQ did not accept the app verification for this sign-in. See [what is known](#what-is-known) |
| MyQ requires a browser verification step that this integration cannot complete | The form encountered a browser challenge; the integration cannot complete this sign-in |
| MyQ presented a sign-in or verification screen this integration does not yet support | The form method met an unsupported step, such as SSO or an unknown verification screen |
| MyQ has limited requests | Wait before trying again; the integration does not retry automatically |
| Could not connect to MyQ | A network error or a MyQ outage |

## What is known

MyQ's sign-in has two halves:

1. **Hosted sign-in.** You enter your password and verification code on
   `partner-identity.myq-cloud.com`, and the page returns a one-time
   authorization code. This has worked in an ordinary browser.
2. **Token exchange.** The app exchanges that code for API tokens. The Android
   app adds a Firebase App Check token, obtained from Google Play Integrity on a
   genuine device, to this request. This integration obtains a Firebase token
   through a registered debug provider instead. It sends the proof in
   `Firebase-AppCheck-Token` on the code exchange only.

Firebase accepted the supplied registration and issued a fresh App Check token.
The registration's app/project differs from the APK's production registration;
MyQ acceptance still needs a complete sign-in test. Earlier MyQ code exchanges
without usable proof failed, including the reported `401.122` rejection.

The form uses unified Android OAuth parameters and PKCE. Its mobile Chrome headers
and native TLS/HTTP2 implementation are described in the
[request profile](research/android-request-profile.md). Browser challenge
execution and complete network equivalence remain unverified. The working
reference also uses Python/aiohttp for hosted pages, so that TLS difference
alone is not established as a login blocker.

## Registration configuration

The private JSON file contains `project`, `application` (Firebase Android app ID),
`api_key`, `debug_token`, `package`, and `certificate` (40 hexadecimal SHA1
characters). These must describe a registered debug provider, not an arbitrary
UUID or the public APK configuration alone. The package is
`com.chamberlain.android.liftmaster.myq`. There is no registration-creation API
call in the integration.

A registered debug token is a credential; it is not embedded or XOR-obfuscated
in source. Keep the file out of public repositories and shared release bundles.
The integration reloads it when acquiring a fresh proof, and stores only the
resulting OAuth session in the Home Assistant entry. A missing, unreadable or
invalid registration is reported before credentials are sent. Firebase rejection
and MyQ rejection are reported separately.

The [authentication reference](api/authentication.md) describes the protocol,
and the [research notes](research/sign-in-investigation.md) record what has been
tried.

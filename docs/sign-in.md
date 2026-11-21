# Sign-in

**Connect MyQ** opens directly to email and password. This is the only sign-in
method in Home Assistant, including when reconnecting an existing account.

1. Open **Settings → Devices & services → Add integration → MyQ**.
2. Enter your MyQ account email and password.
3. Complete the verification prompt if MyQ asks for a method or code.

Your password and verification code are used for this sign-in only and are not
saved. There is no sign-in menu, browser-helper handoff, refresh-token import,
or manual app-verification token field.

End-to-end sign-in remains unverified. MyQ may require a browser challenge or
app-verification proof that the integration cannot currently provide. Simplifying
the form does not remove those server requirements; see [what is known](#what-is-known).

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
   genuine device, to this request. Home Assistant cannot create one, and the
   MyQ web pages inspected do not request one.

The only real exchange tried so far used an early version of this code without
an App Check token, and MyQ refused it with HTTP 401. Its exact reason was not
recorded. The missing App Check token is one possible cause, but that was never
confirmed. The sign-in code has changed a lot since, and the current version
has not been tried.

The form retains the Android OAuth parameters, PKCE and native exchange
[request profile](research/android-request-profile.md). It does not yet acquire
genuine App Check proof. Automatic renewal of an established session remains
implemented; refresh-token import is no longer part of setup.

The [authentication reference](api/authentication.md) describes the protocol,
and the [research notes](research/sign-in-investigation.md) record what has been
tried.

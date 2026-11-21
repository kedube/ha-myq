# Sign-in

MyQ accounts require two-step verification, and MyQ's sign-in ends with a
token exchange that the Android app proves with a Firebase App Check token.
Home Assistant cannot generate that token. Choose the method that matches what
you have.

None of this has been tested against MyQ. No method has completed a sign-in
yet, so session renewal and sign-out have not run against MyQ either. See
[what is known](#what-is-known).

Start from **Settings → Devices & services → Add integration → MyQ**.

## Sign-in methods

### Browser sign-in with app verification

For when you have a current App Check token from an authorized native source.
Home Assistant shows a sign-in request. A helper on a computer with Chrome opens
MyQ's real sign-in pages, where you enter your password and verification code.
The helper then returns a private result that you paste back into Home
Assistant. The token must have more than five minutes of validity left.

Follow the [browser sign-in helper guide](browser-sign-in.md).

### One-time MyQ sign-in link

The MyQ app can sign in from a link of this form:

```text
https://www.myliftmaster.com/ids/auth/onetime-login?token=…
```

Paste the whole link, or only the token. Home Assistant exchanges it for a
session the way the app does; the app sends no App Check token with this
request. Only MyQ's production link hosts are accepted (`www.myliftmaster.com`
and `www.gomerlin.com.au`).

A link works once. Where MyQ issues these links is not known, and the app only
honours them when its `enable_onetime_authorization` feature flag is on.

### Advanced: use an existing refresh token

For a refresh token from your own authorized MyQ session. Home Assistant renews
it immediately and saves the renewed session. MyQ may then stop accepting the
token you pasted, so don't reuse it elsewhere.

### Experimental: email/password form

A diagnostic transport that submits MyQ's sign-in forms directly, without a
browser. It handles the email verification-code step and accepts an optional
App Check token. In trials, MyQ's browser verification page appeared before the
password page and stopped it, so expect it to stop there.

## Finishing setup

After any sign-in, Home Assistant checks your MyQ profile and finds your
devices before it saves the integration. If that check fails, for example
because of a network error, a **Finish connecting MyQ** step lets you retry with
the session you already have. You don't need to sign in again, and a pasted
refresh token or one-time link is not spent on the failed attempt.

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
| MyQ rejected the credentials or session | Wrong password or code, an expired sign-in request, a used one-time link, or a refresh token MyQ no longer accepts |
| Verification was not completed | The verification code was not accepted; enter it again |
| MyQ rejected verification of the application (401.122) | MyQ did not accept the app verification for this sign-in. See [what is known](#what-is-known) |
| A current MyQ app verification token is required | Browser sign-in needs an App Check token; none was supplied |
| The app verification token is expired or has less than five minutes remaining | Get a fresh token and start again |
| The app verification token is malformed | The pasted value is not a token with a readable expiry |
| MyQ requires verification in a real browser | The form method hit MyQ's browser check; use browser sign-in |
| MyQ presented a sign-in or verification screen this integration does not yet support | The form method met an unsupported step, such as SSO or an unknown verification screen |
| That is not a usable one-time MyQ sign-in link | The link is from another host, has no token, or is incomplete |
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

In the app, the one-time link and refresh-token requests carry no App Check
token, so those methods might avoid the problem. Neither has been tried against
MyQ.

The [authentication reference](api/authentication.md) describes the protocol,
and the [research notes](research/sign-in-investigation.md) record what has been
tried.

# Sign-in

You sign in with your MyQ email and password, your verification code, and a
registered Firebase App Check **debug token**. The same form is used to add the
integration and to reconnect it.

- [Before you start: the debug token](#before-you-start-the-debug-token)
- [Connect your account](#connect-your-account)
- [Staying signed in](#staying-signed-in)
- [Removing the integration](#removing-the-integration)
- [Error messages](#error-messages)
- [The registration file](#the-registration-file)

## Before you start: the debug token

MyQ accepts a sign-in only with proof that it comes from the genuine MyQ app.
The Android app gets that proof from Google Play Integrity. This integration
gets it from Firebase App Check with a registered debug token: a reusable
secret registered for one Firebase app. The integration exchanges it for a
short-lived proof at each sign-in.

The token must be registered for this app:

| Identifier | Value |
| --- | --- |
| Firebase project | `myq-transition-test` |
| Firebase Android app ID | `1:169499880894:android:120796f2b5e44ca7` |

These identify the registration; neither is the secret to enter. A token for
another Firebase app, a token from your own Firebase project, or a random UUID
does not work. A MyQ account gives no access to this Firebase project, and the
integration cannot register a token for you.

A debug token is a UUID: 36 characters in five groups separated by hyphens.
It has this form, where each `X` is a digit `0`–`9` or a letter `A`–`F`:

```text
XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
```

Copy it exactly as shown. This is the only value you enter.

**If you already have a registration file**, open your private
`myq_app_check.json` and copy the value of `debug_token`, without the quotes.
In the file it appears as:

```text
"debug_token":"XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
```

A complete file is shown under [the registration file](#the-registration-file).

**If someone with access to the Firebase project is creating a token:**

1. In the [Firebase console](https://console.firebase.google.com/), select the
   project above.
2. Open **Security → App Check → Apps** and find the Android app ID above.
3. In its ⋮ menu, choose **Manage debug tokens** and create a token.
4. Save the secret when it is shown; it cannot be read back later.

See Firebase's [debug provider guide](https://firebase.google.com/docs/app-check/android/debug-provider)
for details.

| Value | Enter in **Debug token**? |
| --- | --- |
| The registered `debug_token` secret | Yes |
| A Firebase App Check token (a JWT) | No; the integration obtains this itself |
| A MyQ email or SMS verification code | No; enter it at the verification step |
| A Firebase API key, app ID or certificate | No; the integration has these |

Keep the debug token private, for example in a password manager. It is not
saved by the integration, and you need it again to reconnect.

## Connect your account

1. Open **Settings → Devices & services → Add integration → MyQ**.
2. Enter your MyQ email, password and debug token. The integration checks the
   debug token with Firebase before sending your password to MyQ.
3. Enter the verification code if MyQ asks for one. Email is chosen when MyQ
   offers it.

The **Debug token** field accepts the token on its own, in quotes, as the
`"debug_token":"…"` entry, or as the whole registration file. If
`myq_app_check.json` is installed beside `configuration.yaml`, you can leave the
field blank to use it; see [the registration file](#the-registration-file).

If Firebase or MyQ limits requests, wait for its cooldown before trying again.
Retrying or restarting setup does not reset that wait. A wait from MyQ applies to
that account's sign-in; a wait from Firebase applies to every account. If MyQ
limits requests while you enter a verification code, enter the same code again
once the wait is over. Retrying after a mistyped password reuses the debug
token's Firebase check rather than asking Firebase again.

After signing in, Home Assistant reads your MyQ profile and devices before
saving the integration. If that fails, for example because of a network error,
a **Finish connecting MyQ** step lets you retry without signing in again.

Each MyQ account can be added once. MyQ's sign-in pages use Home Assistant's
language and country. Your password, debug token and verification code are used
for that sign-in only and are not saved.

## Staying signed in

- The session renews automatically shortly before it expires, and the renewed
  tokens are saved straight away.
- You are asked to sign in again only when MyQ ends the session, as the app
  would sign you out. Home Assistant then shows a **Reconnect MyQ** notice. You
  must sign in to the same MyQ account.
- Other renewal failures keep the session: devices are unavailable until a
  later poll renews it.

## Removing the integration

Deleting the integration ends its MyQ session (waiting at most 15 seconds).
The phone app and other sign-ins stay signed in.

## Error messages

| Message | What to do |
| --- | --- |
| Enter a debug token | Enter the registered debug token, or install `myq_app_check.json` beside `configuration.yaml` |
| Home Assistant cannot read myq_app_check.json | Check that the file exists and that Home Assistant can read it |
| myq_app_check.json is invalid | Restore the complete registration file; a short-lived App Check token does not work there |
| The debug token, app verification configuration, or returned token is invalid | Check the debug token or the registration file |
| Firebase rejected the debug token or app registration | The token is not registered for the project and app above, or has been revoked |
| Could not obtain app verification from Firebase | Check that Home Assistant can reach `firebaseappcheck.googleapis.com`, then try again |
| MyQ rejected the credentials or session | Wrong password or code, or a sign-in that took too long; try again |
| Verification was not completed | The verification code was not accepted; enter it again |
| MyQ rejected verification of the application (401.122) | MyQ refused the app proof; check the debug token and start a new sign-in |
| MyQ requires a browser verification step that this integration cannot complete | MyQ showed a browser check; try again later |
| MyQ presented a sign-in or verification screen this integration does not yet support | For example single sign-on, or an unknown verification screen |
| MyQ denied this request | MyQ refused the sign-in for this account or app; check the debug token |
| MyQ returned an unexpected response | MyQ's sign-in pages or answers were not in the expected form; nothing was saved. The [API reference](api.md#authentication) describes what is expected |
| MyQ has limited requests | Wait before trying again |
| Could not connect to MyQ | A network problem or a MyQ outage |

## The registration file

Instead of entering the debug token each time, you can install a registration
file named `myq_app_check.json` in Home Assistant's configuration directory,
beside `configuration.yaml`, and leave the **Debug token** field blank. It is a
JSON object with:

| Field | Content |
| --- | --- |
| `project` | Firebase project |
| `application` | Firebase Android app ID |
| `api_key` | Firebase API key |
| `debug_token` | The registered debug token |
| `package` | `com.chamberlain.android.liftmaster.myq` |
| `certificate` | Signing certificate SHA-1, 40 hexadecimal characters |

For the registration built into the integration, the file contains this line,
with your debug token in place of the `X` placeholder; every other value is
exactly as shown:

```json
{"project":"myq-transition-test","application":"1:169499880894:android:120796f2b5e44ca7","api_key":"AIzaSyDYwdJBRp6H3UhrCp5LGY8XTPJG7hTeCgw","debug_token":"XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX","package":"com.chamberlain.android.liftmaster.myq","certificate":"DA2BDA70EE8A9062D076BABE65924CAF9A8B98E9"}
```

A file can describe a different Firebase registration from the one built into
the integration. A token entered in the form always uses the built-in app
settings, and takes precedence over the file.

The file is read at each sign-in, so changing it needs no restart. Keep it
private and out of public repositories. It stays outside the integration's
folder, so updates keep it.

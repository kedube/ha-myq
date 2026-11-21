# iPhone capture helper

This development helper observes your own MyQ app session through a local
mitmproxy instance. It is independent of the Home Assistant integration and
does not initiate or replay MyQ requests. No other MyQ integration is used.

USB pairing does not provide decrypted HTTPS headers or remote screen control.
This capture method uses the iPhone's Wi-Fi proxy setting and a temporary trusted
certificate. The Mac and iPhone must be able to reach each other on the local
network. The Mac may use Ethernet. The USB cable can remain connected.

Only `partner-identity.myq-cloud.com` and `firebaseappcheck.googleapis.com` on
port 443 are selected for HTTPS decryption. Other HTTPS destinations pass
through without decryption. The helper saves only selected OAuth protocol
fields, response status, credential-presence flags, and App Check tokens.
It does not save passwords, MFA codes, cookies, authorization codes, PKCE
verifiers, OAuth access/refresh tokens, raw flow files, or raw error messages.
Cleartext HTTP passing through the proxy is visible to the process but is
not saved by the observer. Captured App Check tokens and the generated CA's
private key remain sensitive even though the report is sanitized.
Connection diagnostics include counts of incoming connections, certificate-page
and decrypted auth-host requests, plus MyQ/Firebase CONNECT hostnames. They
exclude client addresses, request URLs, queries, and unrelated hostnames.
While inspection is enabled, the explicit diagnostic URL
`https://firebaseappcheck.googleapis.com/__myq_capture_tls_check` is answered
locally with **MYQ CAPTURE HTTPS CHECK: OK** and a count of checks from another
device. No request for that exact diagnostic path is sent to Firebase. A generic
404 from the Firebase root is not proof of interception, since it also appears
without the proxy. A certificate warning means the TLS check has not succeeded;
report the warning rather than bypassing it.
For a trust check independent of external browser routing, start the helper
with `--tls-check-port 8443` and open its printed local HTTPS address on the
phone, such as `https://192.168.0.24:8443/`. That separate local listener uses a
certificate issued by the same capture CA and serves only a fixed diagnostic.
It sends no MyQ or Firebase requests. The expected text is **MYQ CERTIFICATE
TRUST CHECK: OK**. A certificate warning means the phone has not accepted that
CA for the check. The listener stops with the proxy.
The certificate installer is restricted to the proxy port (8088 by default).
If an HTTP request for the HTTPS diagnostic port passes through the proxy, it
redirects to the explicit HTTPS address instead of showing the installer.

Prepare an isolated environment (already installed on this development Mac):

```sh
.venv/bin/python -m venv analysis/private/capture-venv
analysis/private/capture-venv/bin/python -m pip install 'mitmproxy>=12,<13'
analysis/private/capture-venv/bin/python scripts/capture_native.py --prepare
```

The last command prints a private temporary directory. It generates a fresh
certificate authority, without changing phone/Mac trust or network settings.
Keep the directory on this Mac; do not upload it or add it to Git.

Start the proxy with that directory and the Mac's local IPv4 address:

```sh
analysis/private/capture-venv/bin/python scripts/capture_native.py \
  --session /path/printed/by/prepare --listen-host MAC_LOCAL_IP
```

The default port is 8088 and the session stops after 15 minutes or Ctrl-C.
Use `--minutes 30` for a longer troubleshooting window. The sanitized report
records the run's mode, start, scheduled stop, and stopped/running status so a
test after expiry is not mistaken for an active capture.
The listener binds only the supplied local address. It has no proxy password;
use it only on your trusted local network while performing this capture.
Do not expose or forward its port to the internet.

On the iPhone:

1. Open Settings → Wi-Fi → the information button for the connected network
   → Configure Proxy → Manual. Set Server to the Mac's local IPv4 address,
   Port to 8088, Authentication off, and save.
2. Open the exact local address printed by the helper in Safari, for example
   `http://192.168.0.24:8088/`. This serves the certificate page directly from
   your Mac. The public `mitm.it` domain is no longer needed; an automatic HTTPS
   upgrade there can misleadingly show its "not going through" message even
   with the proxy configured. Download the iOS certificate from this local
   page, then install its profile under Settings → General → VPN &
   Device Management (or Profile Downloaded).
3. Open Settings → General → About → Certificate Trust Settings and enable
   full trust for that mitmproxy certificate. This allows the local proxy to
   read HTTPS for the selected hosts, including login data while it passes
   through memory. Do this only for this capture's certificate.
4. Open MyQ. Start with reopening the app; an existing session may only renew
   its OAuth token. To observe a fresh App Check and authorization exchange,
   sign out and sign back in if needed, completing MFA normally. Stay on
   authentication screens; no door/camera operation is needed.
5. The terminal reports selected exchanges. Share only `sanitized-report.json`
   or its terminal events. App Check values stay in owner-only `app-check-*.txt`
   files in the private capture directory. Do not paste those files into chat.

Trusting the certificate does not guarantee that the native app accepts it:
certificate pinning can prevent interception. If MyQ stops connecting or a TLS
error appears, stop and report that result; do not repeatedly submit credentials.
To restore app connectivity while the phone proxy is still configured, stop the
helper and restart the same command with `--passthrough`. This recovery mode
tunnels all HTTPS without decrypting it, so it cannot capture authentication.
The local certificate page remains available. Turn the phone proxy off before
ending recovery mode or letting its timer expire.
The alternative `--firebase-only` mode decrypts only
`firebaseappcheck.googleapis.com`; MyQ identity HTTPS remains an encrypted
tunnel. This can test whether the native app obtains an App Check token from
Firebase while avoiding interception of the MyQ identity connection. It does
not record the native OAuth exchange and may capture nothing if the app uses
an existing App Check token. Firebase interception itself may also fail TLS.
An iOS token is not assumed to work with the Android OAuth client. The captured
client ID, redirect URI, grant, and provider must be examined together before
adapting or testing any exchange. The helper itself never tries a captured token.

Afterward, turn **Configure Proxy → Off**, remove the installed capture
certificate profile, and stop the Mac helper. These steps also restore normal
network access if the proxy exits or times out. Remove the private capture
directory when its local analysis is finished; it contains the CA private key
and any captured App Check tokens.

Primary references: [mitmproxy domain scoping](https://docs.mitmproxy.org/stable/howto/ignore-domains/),
[certificate setup and pinning](https://docs.mitmproxy.org/stable/concepts/certificates/),
and [Apple's iPhone Mirroring requirements](https://support.apple.com/en-us/120421).

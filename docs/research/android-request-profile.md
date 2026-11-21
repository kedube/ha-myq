# Android authentication request profile

November 21, 2026. This pass re-read the existing JADX CLI 1.5.7 export of MyQ
Android **5.324.0.1132442** (`com.chamberlain.android.liftmaster.myq`). It did not
repeat decompilation or perform a live sign-in. The original export exited 3
with 558 errors; readable methods support the bounded findings below. The base
APK SHA-256 is
`20d6e5d35a819169867f1b7ebbe13769720ae0b78cb6e26ef95bc9f36dabefb2`.

## Verified request details

Source paths are relative to the private export's `sources/` directory.

| Detail | Static source | Implementation |
| --- | --- | --- |
| Native identification | `y92/b.java:232–237` builds `Build.MODEL + "/Android " + Build.VERSION.RELEASE`, `BrandId` and `App-Version`; `ft0/i0.java:129–137` applies the map | Android-format User-Agent plus existing brand and analyzed app version |
| Transport defaults | `okhttp3/internal/http/BridgeInterceptor.java:29–75` adds gzip and HTTP/1 Keep-Alive, with no default `Accept` | Gzip only; HTTP/1 Keep-Alive; suppress automatic Accept and browser-only headers |
| Browser/native cookie separation | `com/chamberlain/myq/sdk/core/network/a.java:192–216,321–324` creates the native client without a cookie-jar override; `okhttp3/OkHttpClient.java:594` and `okhttp3/CookieJar.java:13,24–35` select NoCookies | Native requests exclude browser cookies; curl uses a separate native session without browser impersonation |
| Form serialization | `okhttp3/FormBody.java:35–39` and `okhttp3/internal/url/_UrlKt.java:118–158` | Code exchange, refresh and revocation use `+` for spaces, `%2B` for literal plus, `%7E` for tilde and literal `*` |
| OAuth fields and proof | `ip0/k.java:44–72`, `ip0/s.java:68–78`, `ft0/t0.java:30–48` | Existing Android client, scope, redirect, PKCE and optional App Check header retained; no client-secret field found in this builder |

The selected User-Agent is **`Pixel 9 Pro XL/Android 17`**. Its model/OS pair
comes from the emulator described in the [sign-in investigation](sign-in-investigation.md).
Choosing that pair is an implementation decision: it is not a successful native
capture, a claim about the Home Assistant host, or evidence of an accepted
integrity verdict. The app version remains tied to the analyzed 5.324 APK; the
earlier emulator ran 5.325. Browser navigation retains its existing identity.

## Limits and remaining work

- **TLS and HTTP/2 equivalence is unresolved.** The native client defaults to
  HTTP/2 and HTTP/1.1 (`okhttp3/OkHttpClient.java:11`). Its HTTP/2 codec removes
  connection-specific headers (`okhttp3/internal/http2/Http2ExchangeCodec.java:12,42–64`).
  The socket wrapper enables supported TLS 1.2/1.3 (`ft0/p0.java:16–23`,
  `ft0/q.java:8–16`), despite the TLSv1.2 context name in the builder. Python and
  curl do not thereby acquire Android's TLS handshake or HTTP/2 behavior.
- **App Check acquisition is unresolved.** The integration still consumes proof
  supplied externally. Public app identifiers and device-format headers do not
  supply a signed Play Integrity/App Check result.
- **Backend acceptance is unresolved.** No accepted exchange, usable proof or
  working Android phone was available for this pass. Local transport checks
  establish serialization and isolation only; they cannot establish a fix for
  `401.122` or complete authentication parity.
- **Exact wire ordering is unobserved.** The native code-exchange builder uses
  a Java `HashMap`; source insertion order is not a captured form/header order.

The next decisive evidence remains a successful native exchange with current
proof and its corresponding request metadata. No password/MFA attempt or
physical-device command was made for this change.

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

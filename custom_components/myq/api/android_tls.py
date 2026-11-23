"""The TLS and HTTP/2 profile of the app's OkHttp client on Android 17."""

ANDROID_JA3 = (
    "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,"
    "0-23-65281-10-11-35-16-5-13-51-45-43-21,29-23-24,0"
)
ANDROID_HTTP2 = "4:16777216|16711681|0|m,p,a,s"
ANDROID_EXTRA_FP = {
    "tls_min_version": 6,  # CURL_SSLVERSION_TLSv1_2, with TLS 1.3 also offered.
    "tls_grease": False,
    "tls_permute_extensions": False,
    "tls_cert_compression": "",
    "tls_signature_algorithms": [
        "ecdsa_secp256r1_sha256",
        "rsa_pss_rsae_sha256",
        "rsa_pkcs1_sha256",
        "ecdsa_secp384r1_sha384",
        "rsa_pss_rsae_sha384",
        "rsa_pkcs1_sha384",
        "rsa_pss_rsae_sha512",
        "rsa_pkcs1_sha512",
        "rsa_pkcs1_sha1",
    ],
    "http2_no_priority": True,
}

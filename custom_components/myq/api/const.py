"""Wire constants of MyQ Android 5.324.0.1132442; see docs/api.md."""

IDENTITY = "https://partner-identity.myq-cloud.com"
ACCOUNTS = "https://accounts.myq-cloud.com"
PROFILES = "https://profiles.myq-cloud.com"
DEVICES = "https://devices.myq-cloud.com"
ACTIONS = "https://devices-action.myq-cloud.com"
LAMPS = "https://account-devices-lamp.myq-cloud.com"
GDO = "https://account-devices-gdo.myq-cloud.com"
LOCKS = "https://devices-locks.myq-cloud.com"
PARTNER_DEVICES = "https://myq-partner-devices.myq-cloud.com"
GATEWAYS = "https://gateways.myq-cloud.com"
CREDENTIALS = "https://residential-credentials.myq-cloud.com"
CLIENT_ID = "ANDROID_CGI_MYQ"
SCOPE = "MyQ_Residential offline_access"
REDIRECT_URI = "com.myqops://android"
# Keep equal to manifest.json and pyproject.toml; tests/test_metadata.py enforces it.
VERSION = "0.21"
USER_AGENT = f"ha-myq/{VERSION}"
ANDROID_PACKAGE = "com.chamberlain.android.liftmaster.myq"
APP_VERSION = "5.324.0.1132442"
BRAND_ID = "1"
# Actual Build values, not the AVD's display name. These labels are not attestation.
ANDROID_MODEL = "sdk_gphone64_arm64"
ANDROID_RELEASE = "17"
ANDROID_BUILD = "CE2A.260420.051.A2"
NATIVE_USER_AGENT = f"{ANDROID_MODEL}/Android {ANDROID_RELEASE}"
FIREBASE_USER_AGENT = (
    f"Dalvik/2.1.0 (Linux; U; Android {ANDROID_RELEASE}; {ANDROID_MODEL} Build/{ANDROID_BUILD})"
)
# The working unified flow identifies hosted navigation as mobile Chrome.
# Android 10/K is its reduced browser UA, independent of the native device UA.
HOSTED_BROWSER_VERSION = "153"
HOSTED_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{HOSTED_BROWSER_VERSION}.0.0.0 Mobile Safari/537.36"
)
HOSTED_HEADERS = {
    "User-Agent": HOSTED_USER_AGENT,
    "Sec-CH-UA": f'"Chromium";v="{HOSTED_BROWSER_VERSION}", '
    f'"Google Chrome";v="{HOSTED_BROWSER_VERSION}", "Not_A Brand";v="99"',
    "Sec-CH-UA-Mobile": "?1",
    "Sec-CH-UA-Platform": '"Android"',
}
HOSTED_NAVIGATION_HEADERS = {
    **HOSTED_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}
NATIVE_HEADERS = {
    "User-Agent": NATIVE_USER_AGENT,
    "BrandId": BRAND_ID,
    "App-Version": APP_VERSION,
    # OkHttp BridgeInterceptor defaults; HTTP/2 transports omit Connection.
    "Accept-Encoding": "gzip",
    "Connection": "Keep-Alive",
}
NATIVE_API_BASES = frozenset(
    {
        ACCOUNTS,
        PROFILES,
        DEVICES,
        ACTIONS,
        LAMPS,
        GDO,
        LOCKS,
        PARTNER_DEVICES,
        GATEWAYS,
        CREDENTIALS,
    }
)
TIMEOUT = 30
MAX_RESPONSE = 8 * 1024 * 1024

DOOR_TYPES = frozenset(
    {
        "garagedooropener",
        "wifigaragedooropener",
        "virtualgaragedooropener",
        "commercialdooropener",
        "gate",
    }
)

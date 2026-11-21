"""Wire constants observed in Android 5.324.0.1132442; see docs/api/services.md."""

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
VERSION = "0.00"
USER_AGENT = f"ha-myq/{VERSION}"
# y92/b.s -> ft0/i0: native metadata, not browser navigation headers.
NATIVE_HEADERS = {"BrandId": "1", "App-Version": "5.324.0.1132442"}
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

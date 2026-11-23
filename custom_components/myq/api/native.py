"""Android request formatting, independent of browser sign-in and attestation.

See docs/api.md, Request conventions.
"""

from collections.abc import Mapping
from urllib.parse import quote_plus

from .const import NATIVE_USER_AGENT

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


def uses_native_profile(headers: Mapping[str, str]) -> bool:
    """Only explicitly selected native calls get native transport behavior."""
    return any(
        name.lower() == "user-agent" and value == NATIVE_USER_AGENT
        for name, value in headers.items()
    )


def browser_header(name: str) -> bool:
    """Headers from hosted navigation that the native OkHttp client doesn't send."""
    name = name.lower()
    return name.startswith(("sec-fetch-", "sec-ch-")) or name in {
        "cookie",
        "origin",
        "referer",
        "accept-language",
        "upgrade-insecure-requests",
        "priority",
        "x-requested-with",
    }


def encode_form(fields: Mapping[str, str]) -> bytes:
    """OkHttp FormBody: space -> +, literal *, escaped ~, UTF-8 percent encoding."""

    def encode(value: str) -> str:
        return quote_plus(value, safe="*").replace("~", "%7E")

    return "&".join(f"{encode(key)}={encode(value)}" for key, value in fields.items()).encode(
        "ascii"
    )

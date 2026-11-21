"""Home Assistant errors with translated messages.

The MyQ client is independent of Home Assistant, so its messages stay English. Each kind
of failure gets a translated summary, with the client's message as the detail.
"""

from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .api.errors import (
    AccessDenied,
    AuthenticationError,
    CannotConnect,
    CommandRejected,
    CommandUncertain,
    MyQError,
    ProtocolError,
    RateLimited,
    SessionRenewalFailed,
)
from .const import DOMAIN

# Most specific first: RequestUnauthorized is an AccessDenied, App Check errors are protocol errors.
KINDS: tuple[tuple[type[MyQError], str], ...] = (
    (AuthenticationError, "session_ended"),
    (SessionRenewalFailed, "session_renewal"),
    (RateLimited, "rate_limited"),
    (AccessDenied, "access_denied"),
    (CannotConnect, "cannot_connect"),
    (CommandUncertain, "command_uncertain"),
    (CommandRejected, "command_rejected"),
    (ProtocolError, "unexpected_response"),
)


def translation(err: MyQError) -> dict[str, Any]:
    """Keyword arguments that give any HomeAssistantError a translated message."""
    key = next((key for kind, key in KINDS if isinstance(err, kind)), "myq_error")
    return {
        "translation_domain": DOMAIN,
        "translation_key": key,
        "translation_placeholders": {"detail": str(err)},
    }


def myq_error(err: MyQError) -> HomeAssistantError:
    return HomeAssistantError(**translation(err))


def translated(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )

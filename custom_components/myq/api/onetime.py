"""One-time sign-in links, as the app's verified App Link handler reads them.

The manifest routes https://www.myliftmaster.com/ids/auth/onetime-login?token=...
(and the same path on the other production brand host) to LoginActivity. The app
checks the path (gm0/a.N), reads the "token" query parameter (LoginActivity.L2) and
exchanges it (ip0/j.g); TokenManager.exchange_one_time sends the same request. Where
MyQ issues these links has not been observed; only a link the user pastes is used.
"""

from urllib.parse import parse_qs, urlsplit

from .errors import ProtocolError
from .tokens import MAX_ONE_TIME_TOKEN, validate_one_time_token

# Production hosts in the app's autoVerify intent filter; the others are test stacks.
LINK_HOSTS = frozenset({"www.myliftmaster.com", "www.gomerlin.com.au"})
LINK_PATH = "/ids/auth/onetime-login"


def one_time_token(text: str) -> str:
    """Return the token from a pasted link (or a bare token). Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("Paste the one-time sign-in link")
    text = text.strip()
    if not text or len(text) > MAX_ONE_TIME_TOKEN + 512:
        raise ValueError("Paste the one-time sign-in link")
    if "://" in text:
        parts = urlsplit(text)
        if (
            parts.scheme != "https"
            or (parts.hostname or "").lower() not in LINK_HOSTS
            or parts.port is not None
            or parts.username
            or parts.password
            or LINK_PATH not in parts.path
        ):
            raise ValueError("That is not a MyQ one-time sign-in link")
        values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=32).get("token", [])
        if len(values) != 1:
            raise ValueError("The link does not contain a sign-in token")
        text = values[0]
    try:
        validate_one_time_token(text)
    except ProtocolError:
        raise ValueError("The link does not contain a usable sign-in token") from None
    return text

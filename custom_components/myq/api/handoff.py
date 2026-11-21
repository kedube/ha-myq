"""Bounded browser handoff: public request out, secret callback/proof back.

The OAuthSession and its PKCE verifier stay in Home Assistant. The local
browser helper has only the public authorization URL and transaction expiry.
"""

import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

from .app_check import AppCheckProof
from .const import IDENTITY
from .errors import AuthenticationError, ProtocolError
from .oauth import LOCALE, OAuthSession, validate_callback

MAX_HANDOFF = 32768


def _object(text: str) -> dict:
    try:
        if not isinstance(text, str) or len(text) > MAX_HANDOFF:
            raise ValueError
        value = json.loads(text)
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ValueError
        if value["version"] != 1:
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError("Invalid browser sign-in handoff") from None


def create_request(oauth: OAuthSession) -> str:
    return json.dumps(
        {
            "version": 1,
            "authorize_url": oauth.authorize_url,
            "expires_at": time.time() + max(0, oauth.expires - time.monotonic()),
        },
        separators=(",", ":"),
    )


class BrowserAuthorization:
    """Validate a public request; never acquire or synthesize its verifier."""

    def __init__(self, request: str) -> None:
        value = _object(request)
        try:
            url = value["authorize_url"]
            expiry = value["expires_at"]
            if not isinstance(url, str) or len(url) > 8192:
                raise ValueError
            if type(expiry) not in {int, float} or not math.isfinite(expiry):
                raise ValueError
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.netloc != urlsplit(IDENTITY).netloc
                or parts.path != "/connect/authorize"
                or parts.fragment
            ):
                raise ValueError
            query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=16)
            expected = parse_qs(urlsplit(OAuthSession().authorize_url).query)
            if set(query) != set(expected):
                raise ValueError
            for key, values in query.items():
                if key in {"state", "code_challenge"}:
                    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", values[0]):
                        raise ValueError
                elif key == "ui_locales":
                    # HA sends its own language, as the app sends the phone's.
                    if len(values) != 1 or not LOCALE.fullmatch(values[0]):
                        raise ValueError
                elif values != expected[key]:
                    raise ValueError
            remaining = expiry - time.time()
            if not 0 < remaining <= 601:
                raise AuthenticationError(
                    "Browser sign-in request expired; restart in Home Assistant"
                )
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ProtocolError("Invalid browser sign-in request") from None
        self._url = url
        self.state = query["state"][0]
        self.expires = time.monotonic() + remaining

    def check_active(self) -> None:
        if time.monotonic() >= self.expires:
            raise AuthenticationError("Browser sign-in request expired; restart in Home Assistant")

    @property
    def authorize_url(self) -> str:
        self.check_active()
        return self._url

    def callback_code(self, url: str) -> str:
        self.check_active()
        return validate_callback(url, self.state)


def create_result(request: BrowserAuthorization, callback: str, proof: AppCheckProof) -> str:
    request.callback_code(callback)
    proof.check_fresh()
    return json.dumps(
        {"version": 1, "callback_url": callback, "app_check_token": proof.token},
        separators=(",", ":"),
    )


def read_result(text: str) -> tuple[str, str]:
    value = _object(text)
    callback, token = value.get("callback_url"), value.get("app_check_token")
    if not isinstance(callback, str) or len(callback) > 8192 or not isinstance(token, str):
        raise ProtocolError("Invalid browser sign-in result")
    return callback, token

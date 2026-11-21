"""Read a fresh OAuth redirect from a local browser capture.

Used only by the standalone probe. Passwords, OTPs, cookies, and captured token
responses are never reused. The callback must belong to the current PKCE flow.
"""

import json
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from .auth import identity_url
from .errors import AuthenticationError, ProtocolError, UnsupportedAuthStep
from .handoff import BrowserAuthorization
from .oauth import OAuthSession, is_callback

MAX_HAR_BYTES = 64 * 1024 * 1024


def read_callback(path: Path, oauth: OAuthSession | BrowserAuthorization) -> str:
    """Return one validated identity-origin redirect; never emit raw HAR content."""
    oauth.check_active()
    try:
        with path.expanduser().open("rb") as handle:
            raw = handle.read(MAX_HAR_BYTES + 1)
        if len(raw) > MAX_HAR_BYTES:
            raise ProtocolError("HAR exceeds the 64 MiB capture limit")
        capture = json.loads(raw)
    except (OSError, RuntimeError):
        raise ProtocolError("Could not read the local HAR file") from None
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("The selected file is not a valid HAR") from None
    try:
        entries = capture["log"]["entries"]
    except (KeyError, TypeError):
        raise ProtocolError("The selected HAR has no network entries") from None
    if not isinstance(entries, list):
        raise ProtocolError("The selected HAR has invalid network entries")
    callbacks = set()
    other_transaction = False
    for entry in entries:
        try:
            source = entry["request"]["url"]
            response = entry["response"]
            identity_url(source)
            if response["status"] not in {301, 302, 303, 307, 308}:
                continue
            locations = [
                header["value"]
                for header in response["headers"]
                if header["name"].lower() == "location"
            ]
            if len(locations) != 1:
                continue
            target = urljoin(source, locations[0])
            if not is_callback(target):
                continue
            query = parse_qs(urlsplit(target).query, keep_blank_values=True)
        except (KeyError, TypeError, ValueError, AttributeError, ProtocolError):
            continue
        if query.get("state") != [oauth.state]:
            other_transaction = True
            continue
        # Reject errors, fragments, ambiguous codes, expiry, or a used flow.
        oauth.callback_code(target)
        callbacks.add(target)
    if len(callbacks) > 1:
        raise ProtocolError("HAR contains multiple OAuth callbacks for this sign-in")
    if not callbacks:
        if other_transaction:
            raise AuthenticationError("HAR callback does not match this probe's sign-in session")
        raise UnsupportedAuthStep(
            "HAR contains no MyQ OAuth callback; website login alone is not an API session"
        )
    oauth.check_active()
    return callbacks.pop()

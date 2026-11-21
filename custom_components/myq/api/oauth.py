"""OAuth transaction independent of the browser or diagnostic form transport.

Parameters follow sp0/b -> cp0/d -> ip0/j.c -> ip0/n.h -> AuthUrlParams.
The legacy hidden-WebView flow uses ip0/j.j0 instead (unifiedFlow=false).
"""

import asyncio
import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .app_check import AppCheckProof, AppCheckProvider, validate_app_check_token
from .const import CLIENT_ID, IDENTITY, REDIRECT_URI, SCOPE
from .errors import (
    AppCheckError,
    AppCheckInvalid,
    AppCheckUnavailable,
    ApplicationVerificationError,
    AuthenticationError,
    ProtocolError,
)
from .tokens import TokenManager, Tokens

DEFAULT_LOCALE = "en-US"
LOCALE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}")
COUNTRY = re.compile(r"[A-Za-z]{2}")


def ui_locale(language: str | None, country: str | None = None) -> str:
    """The app's ui_locales value (es0/c.c): language, plus "-" and country when known.

    Home Assistant languages may already carry a region ("en-GB", "pt-BR"); a bare
    language takes HA's configured country. Anything unusable falls back to en-US.
    """
    tag = (language or "").strip().replace("_", "-")
    if tag and "-" not in tag and isinstance(country, str) and COUNTRY.fullmatch(country):
        tag = f"{tag}-{country.upper()}"
    return tag if LOCALE.fullmatch(tag) else DEFAULT_LOCALE


def is_callback(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "com.myqops" and parsed.netloc == "android"


def validate_callback(url: str, state: str) -> str:
    """Validate an exact callback without sharing the private PKCE verifier."""
    if not isinstance(url, str) or len(url) > 8192:
        raise ProtocolError("Unexpected MyQ authorization callback")
    try:
        parsed = urlsplit(url)
        if not is_callback(url) or parsed.path not in {"", "/"} or parsed.fragment:
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        raise ProtocolError("Unexpected MyQ authorization callback") from None
    if query.get("state") != [state]:
        raise AuthenticationError("OAuth state did not match this sign-in session")
    if "iss" in query and query["iss"] != [IDENTITY]:
        raise AuthenticationError("OAuth issuer did not match MyQ")
    if "error" in query:
        raise AuthenticationError("MyQ sign-in was not completed")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise ProtocolError("Missing or ambiguous OAuth authorization code")
    return codes[0]


class OAuthSession:
    """Bind a single code exchange to a state, PKCE verifier, and lifetime."""

    def __init__(self, *, unified: bool = True, locale: str = DEFAULT_LOCALE) -> None:
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + 600
        self.unified = unified
        self.locale = locale if LOCALE.fullmatch(locale) else DEFAULT_LOCALE
        self.used = False
        self._completion_lock = asyncio.Lock()

    def check_active(self) -> None:
        if self.used:
            raise AuthenticationError("This sign-in transaction has already been used")
        if time.monotonic() >= self.expires:
            raise AuthenticationError("Sign-in session expired; start again")

    @property
    def authorize_url(self) -> str:
        self.check_active()
        digest = hashlib.sha256(self.verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        # AuthUrlParams.isMFA is false for sign-in. Bare "unified_flow" belongs
        # to the separate authenticated MFA-settings URL, not this login path.
        return (
            IDENTITY
            + "/connect/authorize?"
            + urlencode(
                {
                    "client_id": CLIENT_ID,
                    "scope": SCOPE,
                    "response_type": "code",
                    "redirect_uri": REDIRECT_URI,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "ui_locales": self.locale,
                    "acr_values": ("unified_flow:v1" if self.unified else "") + "  brand:myq",
                    "prompt": "login",
                    "state": self.state,
                }
            )
        )

    def callback_code(self, url: str) -> str:
        self.check_active()
        return validate_callback(url, self.state)

    def _claim(self) -> None:
        # Called under the token-manager lock, immediately before HTTP I/O.
        self.check_active()
        self.used = True

    async def complete(
        self,
        url: str,
        manager: TokenManager,
        *,
        app_check_token: str | None = None,
        app_check_provider: AppCheckProvider | None = None,
        require_app_check: bool = False,
    ) -> Tokens:
        async with self._completion_lock:
            code = self.callback_code(url)
            if app_check_token is not None and app_check_provider is not None:
                raise ProtocolError("Choose one App Check proof source")
            if require_app_check and app_check_provider is None and app_check_token is None:
                raise AppCheckUnavailable()
            if app_check_token is not None:
                validate_app_check_token(app_check_token)
            proof = None
            if app_check_provider is not None:
                try:
                    async with asyncio.timeout(30):
                        proof = await app_check_provider.acquire()
                    if not isinstance(proof, AppCheckProof):
                        raise AppCheckInvalid()
                except AppCheckError:
                    raise
                except Exception:
                    raise AppCheckUnavailable() from None
            self.check_active()
            try:
                return await manager.exchange_code(
                    code,
                    self.verifier,
                    app_check_token=app_check_token,
                    proof=proof,
                    before_send=self._claim,
                )
            except ApplicationVerificationError:
                if app_check_provider is not None:
                    app_check_provider.invalidate()
                raise

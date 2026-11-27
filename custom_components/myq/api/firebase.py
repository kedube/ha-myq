"""Exchange a registered Firebase debug token for App Check.

Registration with the Firebase project is a prerequisite. This implements the
debug-provider REST exchange, not Google Play Integrity or token registration.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .app_check import AppCheckProof, CachedAppCheckProvider
from .const import ANDROID_PACKAGE, FIREBASE_USER_AGENT
from .errors import (
    AppCheckConfigInvalid,
    AppCheckConfigUnreadable,
    AppCheckIntegrityError,
    AppCheckInvalid,
    AppCheckNotConfigured,
    AppCheckUnavailable,
)
from .http import HttpTransport, check_status

REGISTRATION_FILE = "myq_app_check.json"
FIREBASE_BASE = "https://firebaseappcheck.googleapis.com/v1"
MAX_REGISTRATION_BYTES = 16384

# Public app identifiers for the registered debug provider used by this integration.
# This is distinct from the production APK's Play Integrity registration.
# The reusable debug credential is supplied at sign-in and is never bundled here.
_MYQ_APP = {
    "project": "myq-transition-test",
    "application": "1:169499880894:android:120796f2b5e44ca7",
    "api_key": "AIzaSyDYwdJBRp6H3UhrCp5LGY8XTPJG7hTeCgw",
    "certificate": "DA2BDA70EE8A9062D076BABE65924CAF9A8B98E9",
    "package": "com.chamberlain.android.liftmaster.myq",
}


@dataclass(frozen=True, slots=True, repr=False)
class FirebaseRegistration:
    """Provider configuration with a private token; never included in repr or HA data."""

    project: str
    application: str
    api_key: str
    debug_token: str
    certificate: str
    package: str = ANDROID_PACKAGE

    def __post_init__(self) -> None:
        patterns = {
            "project": r"[A-Za-z0-9-]{1,128}",
            "application": r"1:[0-9]+:android:[a-fA-F0-9]+",
            "api_key": r"[A-Za-z0-9_-]{1,256}",
            "debug_token": r"[!-~]{1,1024}",
            "certificate": r"[a-fA-F0-9]{40}",
        }
        for name, pattern in patterns.items():
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise AppCheckInvalid()
        if self.package != ANDROID_PACKAGE:
            raise AppCheckInvalid()
        if self.project.isdigit() and self.project != self.application.split(":")[1]:
            raise AppCheckInvalid()

    @property
    def resource(self) -> str:
        return f"projects/{self.project}/apps/{self.application}:exchangeDebugToken"

    @classmethod
    def from_debug_token(cls, debug_token: str) -> FirebaseRegistration:
        """Accept a raw credential or its JSON representation copied from a file."""
        if not isinstance(debug_token, str) or len(debug_token) > MAX_REGISTRATION_BYTES:
            raise AppCheckInvalid()
        value = debug_token.strip()
        if value.startswith(('"', "{", "[", "'")):
            try:
                # A copied property or quoted value can include its trailing comma.
                encoded = value.removesuffix(",").rstrip()
                try:
                    decoded = json.loads(encoded)
                except ValueError:
                    # A single property needs its containing object's braces.
                    if not encoded.startswith('"debug_token"'):
                        raise
                    decoded = json.loads("{" + encoded + "}")
                token = decoded.get("debug_token") if isinstance(decoded, dict) else decoded
                if not isinstance(token, str):
                    raise ValueError
                value = token.strip()
            except (ValueError, RecursionError):
                raise AppCheckInvalid() from None
        return cls(**_MYQ_APP, debug_token=value)

    @classmethod
    def read(cls, path: Path) -> FirebaseRegistration:
        """Bound reads and reject nonregular files before reading; redact errors."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise AppCheckInvalid()
                raw = handle.read(MAX_REGISTRATION_BYTES + 1)
            if len(raw) > MAX_REGISTRATION_BYTES:
                raise AppCheckInvalid()
            fields = json.loads(raw)
            if not isinstance(fields, dict):
                raise AppCheckInvalid()
            return cls(**fields)
        except FileNotFoundError:
            raise AppCheckNotConfigured() from None
        except OSError:
            raise AppCheckConfigUnreadable() from None
        except (AppCheckInvalid, ValueError, TypeError, UnicodeError, RecursionError):
            raise AppCheckConfigInvalid() from None


class FirebaseDebugAppCheckProvider(CachedAppCheckProvider):
    """Acquire and refresh proof, sharing concurrent callers and keeping it in RAM."""

    def __init__(self, http: HttpTransport, registration: FirebaseRegistration | Path) -> None:
        async def load() -> AppCheckProof:
            profile = (
                await asyncio.to_thread(FirebaseRegistration.read, registration)
                if isinstance(registration, Path)
                else registration
            )
            started = time.time()
            response = await http.request(
                "POST",
                FIREBASE_BASE + "/" + profile.resource + "?key=" + quote(profile.api_key, safe=""),
                headers={
                    "User-Agent": FIREBASE_USER_AGENT,
                    "Content-Type": "application/json",
                    "X-Android-Package": profile.package,
                    # er1/o.d -> Google AndroidUtilsLight: uppercase SHA1, not SHA256.
                    "X-Android-Cert": profile.certificate.upper(),
                },
                json={"debugToken": profile.debug_token},
            )
            if response.status == 429:
                check_status(response)
            if response.status in {401, 403}:
                raise AppCheckIntegrityError()
            if response.status != 200:
                raise AppCheckUnavailable()
            try:
                data = response.json()
                proof = AppCheckProof.from_token(data["token"])
                ttl = data["ttl"]
                if not isinstance(ttl, str) or not re.fullmatch(
                    r"[0-9]{1,12}(?:\.[0-9]{1,9})?s", ttl
                ):
                    raise ValueError
                payload = proof.token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                project_number = profile.application.split(":")[1]
                audience = claims.get("aud")
                if isinstance(audience, str):
                    audience = [audience]
                if (
                    claims.get("sub") != profile.application
                    or not isinstance(audience, list)
                    or f"projects/{project_number}" not in audience
                ):
                    raise ValueError
                # Local claim checks catch registration mixups; they do not verify
                # a JWT signature. The HTTPS Firebase response supplies the proof.
                return AppCheckProof(proof.token, min(proof.expires_at, started + float(ttl[:-1])))
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                raise AppCheckInvalid() from None

        super().__init__(load)

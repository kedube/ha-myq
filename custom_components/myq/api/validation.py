"""Read-only session checks; real authentication still requires accepted proof."""

from collections import Counter

from .client import Client
from .errors import AuthenticationError
from .management import Management


async def validate_session(
    client: Client, *, expected_user_id: str | None = None, refresh: bool = False
) -> dict:
    """Check identity, discovery, permissions and optional rotation without actuation.

    TokenManager persists rotations through its existing on-update callback before
    returning. This result contains counts and roles, never tokens or identifiers.
    """
    before = await client.profile()
    identity = before["user_id"]
    if expected_user_id is not None and identity != expected_user_id:
        raise AuthenticationError("MyQ session belongs to a different user; sign in again")
    if refresh:
        current = await client.auth.access_token()
        await client.auth.access_token(rejected_token=current, force=True)
        if (await client.profile())["user_id"] != identity:
            raise AuthenticationError("MyQ identity changed after session renewal")
    devices = await client.discover()
    access = await Management(client).account_access()
    return {
        "profile_verified": True,
        "refresh_verified": refresh,
        "account_count": len(access),
        "device_count": len(devices),
        "device_types": dict(sorted(Counter(d.device_type for d in devices.values()).items())),
        "account_roles": dict(sorted(Counter(a.role for a in access.values()).items())),
    }

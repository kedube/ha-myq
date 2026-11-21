"""Native AccountAccess roles, independent of device discovery.

Evidence: an0/a, zm0/b, bn0/AccountAccessEntry, cn0/a, cn0/d, rm0/AccountRole and
rm0/AccountPermissions. The app parses each account's permission lists but never reads
them: AccountPermissions has no reader, and every screen is gated on the role. Owner is
not a role check at all (myq/sdk/common/misc/h0.k): an account is owned when its
created_by is the signed-in user. Unknown roles never confer access.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from .errors import ProtocolError

ROLES = frozenset({"owner", "co_owner", "guest", "family", "guest_nomyquser"})
PERMISSION_LEVELS = frozenset({"All", "Manage", "View", "Control", "Delete"})
RESOURCES = frozenset(
    {"accounts", "alerts", "devices", "guests", "history", "schedules", "partners"}
)
OWNER_OR_CO_OWNER = frozenset({"owner", "co_owner"})
NOT_GUEST = ROLES - {"guest", "guest_nomyquser"}
# The roles whose app screens offer each kind of operation.
ROLE_GATES: dict[str, frozenset[str]] = {
    # Live view, camera controls and recordings have no role check in the app.
    "devices": ROLES,
    # The Notifications row is shown to every role (y80/g.o).
    "alerts": ROLES,
    # The History and Users tabs are hidden only for guests (dm0/d, dm0/e).
    "history": NOT_GUEST,
    "guests": NOT_GUEST,
    # Camera schedules are reached only from owner-only camera settings (y80/i, y80/k).
    "schedules": frozenset({"owner"}),
}


@dataclass(frozen=True)
class AccountAccess:
    account_id: str = field(repr=False)
    role: str
    # Parsed for completeness; the app never reads them, so nothing is gated on them.
    permissions: dict[str, frozenset[str]] = field(repr=False, default_factory=dict)
    created: bool = False

    @property
    def owner(self) -> bool:
        return self.created or self.role == "owner"

    @property
    def effective_role(self) -> str:
        return "owner" if self.owner else self.role

    @property
    def owner_or_co_owner(self) -> bool:
        return self.effective_role in OWNER_OR_CO_OWNER

    @property
    def can_control_devices(self) -> bool:
        return self.allows("devices", "Control")

    def allows(self, resource: str, permission: str) -> bool:
        """Whether the app offers this kind of operation to this account's role."""
        return permission in PERMISSION_LEVELS and self.effective_role in ROLE_GATES.get(
            resource, frozenset()
        )


def owned_access(owned: Collection[str]) -> dict[str, AccountAccess]:
    """What is known without AccountAccess: accounts the signed-in user created."""
    return {account_id: AccountAccess(account_id, "owner", created=True) for account_id in owned}


def parse_account_access(data: Any, owned: Collection[str] = ()) -> dict[str, AccountAccess]:
    """cn0/a and cn0/d, reading role and level names without regard to case."""
    if not isinstance(data, dict) or not isinstance(data.get("account_access"), list):
        raise ProtocolError("MyQ account permissions response is invalid")
    result = {}
    for item in data["account_access"]:
        if not isinstance(item, dict):
            raise ProtocolError("MyQ account permissions response is invalid")
        account_id = item.get("account_id")
        if not isinstance(account_id, str) or not account_id or account_id in result:
            raise ProtocolError("MyQ account permissions contain an ambiguous account")
        role = item.get("role")
        role = role.strip().lower() if isinstance(role, str) else ""
        # The app rejects QuickPass and unknown roles in AccountAccessMapper.
        role = role if role in ROLES else "unknown"
        raw_permissions = item.get("permissions")
        if raw_permissions is None:
            raw_permissions = {}
        if not isinstance(raw_permissions, dict):
            raise ProtocolError("MyQ account permissions response is invalid")
        levels_by_name = {level.lower(): level for level in PERMISSION_LEVELS}
        permissions = {}
        for resource in RESOURCES:
            levels = raw_permissions.get(resource, [])
            if levels is None:
                levels = []
            if not isinstance(levels, list) or any(not isinstance(x, str) for x in levels):
                raise ProtocolError("MyQ account permission levels are invalid")
            permissions[resource] = frozenset(
                levels_by_name[x.lower()] for x in levels if x.lower() in levels_by_name
            )
        result[account_id] = AccountAccess(
            account_id, role, permissions, created=account_id in owned
        )
    for account_id, access in owned_access(owned).items():
        result.setdefault(account_id, access)
    return result

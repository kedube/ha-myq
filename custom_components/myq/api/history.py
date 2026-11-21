"""Read-only device history (accounthistory v6.1), as the app's history screen reads it.

Evidence: d61/a (host), h61/d and g61/n (route), j61/DeviceHistoryListDto, k61/c
(event types and timestamps). The account-wide DELETE route is never called.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .client import Client
from .const import NATIVE_HEADERS
from .errors import CommandRejected, ProtocolError

ACCOUNTHISTORY = "https://accounthistory.myq-cloud.com"
# y51/t device events; anything else is reported as "other".
DEVICE_EVENTS = (
    "open",
    "closed",
    "on",
    "off",
    "locked",
    "unlocked",
    "stopped",
    "accessed",
    "call_answered",
    "call_unanswered",
    "expired",
    "attempted",
    "assigned_remote",
    "unassigned_remote",
    "failed_to_close",
    "failed_to_lock",
)
MAX_SERIALS = 100


def parse_time(value: Any) -> datetime | None:
    """ds0/a tries yyyy-MM-dd'T'HH:mm:ss:SSS'Z' first, then ISO variants; UTC if no zone."""
    if not isinstance(value, str) or not 19 <= len(value) <= 40:
        return None
    value = re.sub(r"^(.{19}):(\d{3})(Z?)$", r"\1.\2\3", value.strip())
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _text(data: Any, key: str) -> str | None:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) and value and len(value) <= 256 else None


@dataclass(frozen=True)
class HistoryEvent:
    """One entry. User identifiers are dropped; names are kept as the app shows them."""

    id: str = field(repr=False)
    timestamp: datetime
    event_type: str
    device_event: str
    device_serial: str | None = field(default=None, repr=False)
    device_name: str | None = field(default=None, repr=False)
    by: str | None = field(default=None, repr=False)
    source: str | None = None
    partner: str | None = field(default=None, repr=False)
    schedule: str | None = field(default=None, repr=False)
    credential_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type,
            "device_event": self.device_event,
            "device_serial": self.device_serial,
            "device_name": self.device_name,
            "by": self.by,
            "source": self.source,
            "partner": self.partner,
            "schedule": self.schedule,
            "credential_type": self.credential_type,
        }


@dataclass(frozen=True)
class HistoryPage:
    events: tuple[HistoryEvent, ...]
    continuation: str | None = field(default=None, repr=False)


def parse_page(data: Any) -> HistoryPage:
    if not isinstance(data, dict):
        raise ProtocolError("MyQ device history response is invalid")
    rows = data.get("device_history")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ProtocolError("MyQ device history response is invalid")
    events, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ProtocolError("MyQ device history entry is invalid")
        identifier, timestamp = _text(row, "id"), parse_time(row.get("event_timestamp"))
        if identifier is None or timestamp is None or identifier in seen:
            continue  # The app skips entries it cannot place in time.
        seen.add(identifier)
        event, by = row.get("event"), row.get("event_by")
        device_event = (_text(event, "device_event") or "").lower()
        events.append(
            HistoryEvent(
                identifier,
                timestamp,
                (_text(row, "event_type") or "unknown").lower(),
                device_event if device_event in DEVICE_EVENTS else "other",
                _text(event, "device_serial_number"),
                _text(event, "device_name"),
                _text(by, "name"),
                _text(by, "source"),
                _text(by, "partner_name"),
                _text(event, "schedule_name"),
                _text(event, "credential_type"),
            )
        )
    token = data.get("continuation_token")
    return HistoryPage(
        tuple(sorted(events, key=lambda item: item.timestamp)),
        token if isinstance(token, str) and token and len(token) <= 4096 else None,
    )


class History:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def read(
        self, account_id: str, serials: list[str], continuation: str | None = None
    ) -> HistoryPage:
        serials = sorted(set(serials))
        if (
            not serials
            or len(serials) > MAX_SERIALS
            or any(not isinstance(s, str) or not s or "," in s for s in serials)
        ):
            raise CommandRejected("Device history needs registered device serial numbers")
        params = {"devices": ",".join(serials)}
        if continuation is not None:
            if not isinstance(continuation, str) or not 0 < len(continuation) <= 4096:
                raise CommandRejected("Invalid device history continuation token")
            params["continue"] = continuation
        response = await self.client.request(
            "GET",
            ACCOUNTHISTORY,
            f"/api/v6.1/accounts/{quote(account_id, safe='')}/deviceHistory",
            headers=NATIVE_HEADERS,
            params=params,
        )
        return parse_page(response.json())

"""Notification rules and account snooze (alerts v6.1 / v6.0), from fo0/a and eo0/m.

A rule is written back as the complete AlertDto it was read as (ho0/a), changing
only is_enabled. Creating and deleting rules is not offered.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .client import Client
from .const import NATIVE_HEADERS
from .errors import CommandRejected, ProtocolError
from .models import Device

ALERTS = "https://alerts.myq-cloud.com"
ALERT_FIELDS = (
    "id",
    "serial_number",
    "display_name",
    "is_enabled",
    "linked_alert_id",
    "details",
    "schedules",
    "is_auto_created",
)
MAX_SNOOZE_MINUTES = 7 * 24 * 60


@dataclass(frozen=True)
class AlertRule:
    id: str = field(repr=False)
    serial: str = field(repr=False)
    name: str = field(repr=False)
    enabled: bool
    dto: dict[str, Any] = field(repr=False, compare=False)

    def summary(self) -> dict[str, Any]:
        events = [
            item.get("event_type")
            for item in self.dto.get("details") or []
            if isinstance(item, dict) and isinstance(item.get("event_type"), str)
        ]
        return {"id": self.id, "name": self.name, "enabled": self.enabled, "events": events}


def parse_rules(data: Any) -> tuple[AlertRule, ...]:
    """AlertsListDto is {"items": [...]} with no count."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ProtocolError("MyQ alert rules response is invalid")
    rules, seen = [], set()
    for item in data["items"]:
        if not isinstance(item, dict):
            raise ProtocolError("MyQ alert rule is invalid")
        identifier, serial = item.get("id"), item.get("serial_number")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ProtocolError("MyQ alert rules contain an ambiguous identity")
        seen.add(identifier)
        enabled = item.get("is_enabled")
        if not isinstance(serial, str) or not serial or type(enabled) is not bool:
            continue  # Account-level or malformed rules grant no per-device switch.
        name = item.get("display_name")
        rules.append(
            AlertRule(
                identifier,
                serial,
                name if isinstance(name, str) and name.strip() else "Alert",
                enabled,
                {key: deepcopy(item[key]) for key in ALERT_FIELDS if key in item},
            )
        )
    return tuple(rules)


def snooze_time(value: datetime) -> str:
    """ho0/f writes yyyy-MM-dd'T'HH:mm:ss.SSS'Z' in UTC."""
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class Alerts:
    def __init__(self, client: Client) -> None:
        self.client = client

    @staticmethod
    def _path(account_id: str) -> str:
        return f"/api/v6.1/accounts/{quote(account_id, safe='')}/alerts"

    async def rules(self, account_id: str) -> tuple[AlertRule, ...]:
        response = await self.client.request(
            "GET", ALERTS, self._path(account_id), headers=NATIVE_HEADERS
        )
        return parse_rules(response.json())

    async def rule(self, device: Device, rule_id: str) -> AlertRule:
        for rule in await self.rules(device.account_id):
            if rule.id == rule_id and rule.serial == device.serial:
                return rule
        raise CommandRejected("Alert rule is no longer associated with this device")

    async def set_enabled(self, device: Device, rule: AlertRule, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise CommandRejected("Alert state must be a Boolean")
        body = {**deepcopy(rule.dto), "is_enabled": enabled}
        await self.client.device_mutation(
            device,
            "PUT",
            ALERTS,
            self._path(device.account_id) + "/" + quote(rule.id, safe=""),
            headers=NATIVE_HEADERS,
            json=body,
        )

    @staticmethod
    def _snooze_path(account_id: str) -> str:
        return f"/api/v6.0/accounts/{quote(account_id, safe='')}/alerts/snooze"

    async def snooze(self, device: Device, minutes: int) -> dict[str, str]:
        if type(minutes) is not int or not 1 <= minutes <= MAX_SNOOZE_MINUTES:
            raise CommandRejected("Snooze for 1 minute to 7 days")
        now = datetime.now(UTC)
        body = {
            "created_at": snooze_time(now),
            "expires_at": snooze_time(now + timedelta(minutes=minutes)),
        }
        await self.client.device_mutation(
            device,
            "POST",
            ALERTS,
            self._snooze_path(device.account_id),
            headers=NATIVE_HEADERS,
            json=body,
        )
        return body

    async def cancel_snooze(self, device: Device) -> None:
        await self.client.device_mutation(
            device, "DELETE", ALERTS, self._snooze_path(device.account_id), headers=NATIVE_HEADERS
        )

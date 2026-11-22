"""Account, device, and command endpoints observed in the Android application."""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

from .android_http import AndroidHttp
from .backoff import ServiceBackoff
from .const import ACCOUNTS, ACTIONS, DEVICES, LAMPS, NATIVE_API_BASES, NATIVE_HEADERS, PROFILES
from .errors import CannotConnect, CommandRejected, CommandUncertain, ProtocolError, RateLimited
from .exclusive import ExclusiveLocks
from .http import HttpTransport, Response, check_status, renewable
from .models import Account, Device, object_list
from .tokens import TokenManager, Tokens


class Client:
    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        tokens: Tokens | None = None,
        on_token_update: Callable[[Tokens], Awaitable[None]] | None = None,
        *,
        transport: HttpTransport | None = None,
        backoff: ServiceBackoff | None = None,
    ) -> None:
        if transport is not None:
            self.http = transport
        elif session is not None:
            self.http = AndroidHttp(session, backoff)
        else:
            raise ValueError("A session or HTTP transport is required")
        self.auth = TokenManager(self.http, tokens, on_token_update)
        self._command_locks = ExclusiveLocks()
        # The last accounts list, for owner checks that need no extra request.
        self.known_accounts: tuple[Account, ...] = ()
        # One deadline per origin: share the transport's store when it keeps one.
        # Injected transports that return 429 instead of raising get a local store.
        shared = getattr(self.http, "backoff", None)
        self.backoff = shared if isinstance(shared, ServiceBackoff) else ServiceBackoff()

    async def close(self) -> None:
        """Release owned native pools; injected transports and HA's session stay open."""
        if isinstance(self.http, AndroidHttp):
            await self.http.close()

    async def request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        self.backoff.check(base)
        headers = {**(NATIVE_HEADERS if base in NATIVE_API_BASES else {}), **(headers or {})}
        token = await self.auth.access_token()
        response = await self._send(
            method,
            base,
            path,
            headers={**headers, "Authorization": "Bearer " + token},
            **kwargs,
        )
        # ft0/c1 renews and sends once more, for any method: a 401 means MyQ did not
        # act on the request. A second 401 fails only this request (RequestUnauthorized).
        if renewable(response):
            token = await self.auth.access_token(rejected_token=token)
            response = await self._send(
                method,
                base,
                path,
                headers={**headers, "Authorization": "Bearer " + token},
                **kwargs,
            )
        check_status(response)
        return response

    async def _send(self, method: str, base: str, path: str, **kwargs: Any) -> Response:
        self.backoff.check(base)
        try:
            response = await self.http.request(method, base + path, **kwargs)
            if response.status == 429:
                check_status(response)
            return response
        except RateLimited as err:
            # Auth refresh has its own identity origin and is deliberately outside
            # this boundary. Never attribute its quota to a device service.
            self.backoff.record(base, err)
            raise

    async def profile(self) -> dict[str, Any]:
        data = (await self.request("GET", PROFILES, "/api/v6.0/my/profile")).json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("user_id"), str)
            or not data["user_id"]
        ):
            raise ProtocolError("MyQ profile did not contain a user ID")
        return data

    async def accounts(self) -> list[Account]:
        data = (await self.request("GET", ACCOUNTS, "/api/v6.0/accounts")).json()
        accounts = [Account.parse(row) for row in object_list(data, "accounts")]
        self.known_accounts = tuple(accounts)
        return accounts

    async def devices(self, account_id: str) -> list[Device]:
        path = f"/api/v6.0/Accounts/{quote(account_id, safe='')}/Devices"
        data = (await self.request("GET", DEVICES, path)).json()
        rows = object_list(data, "items")
        # The app's DeviceList has count/href but no paging parameter. Do not
        # silently claim complete discovery if the server starts paginating.
        if isinstance(data.get("count"), int) and data["count"] > len(rows):
            raise ProtocolError(
                "MyQ returned a partial device list; pagination needs investigation"
            )
        return [Device.parse(account_id, row) for row in rows]

    async def discover(self, *, reuse_accounts: bool = False) -> dict[str, Device]:
        """Devices of every account; reuse_accounts skips re-reading the accounts list."""
        devices = {}
        accounts = (
            self.known_accounts if reuse_accounts and self.known_accounts else await self.accounts()
        )
        # Sequential per-account requests avoid a cloud request burst.
        for account in accounts:
            for device in await self.devices(account.id):
                devices[device.key] = device
        return devices

    async def device_mutation(
        self, device: Device, method: str, base: str, path: str, **kwargs: Any
    ) -> Response:
        """Serialize a validated device operation without queuing or blind replay.

        Only a 401 answer is sent again, once, after renewal (see request()).
        """
        if method not in {"PUT", "POST", "DELETE"}:
            raise ProtocolError("Invalid device mutation method")
        async with self._command_locks.hold(
            device.key, "A command for this device is already in progress"
        ):
            # Renew before anything is sent, so a renewal outage is not mistaken for
            # a command whose outcome is unknown.
            await self.auth.access_token()
            try:
                return await self.request(method, base, path, **kwargs)
            except CannotConnect:
                raise CommandUncertain(
                    "Device command outcome is unknown; refresh its state before trying again"
                ) from None

    async def command(self, device: Device | None, action: str) -> None:
        if device is None:
            raise CommandRejected("Device is no longer registered")
        device.validate_command(action)
        path = (
            f"/api/v1.0/accounts/{quote(device.account_id, safe='')}/devices/"
            f"{quote(device.serial, safe='')}"
        )
        await self.device_mutation(
            device,
            "PUT",
            ACTIONS,
            path,
            json={"action": action, "trigger": "myq_an", "device_family": "garagedoor"},
        )

    async def lamp(self, device: Device | None, enabled: bool) -> None:
        if device is None or not device.is_lamp or not device.online or type(enabled) is not bool:
            raise CommandRejected("Lamp is unavailable")
        if (reason := device.control_blocked()) is not None:
            raise CommandRejected(reason)
        action = "on" if enabled else "off"
        path = (
            f"/api/v6.0/accounts/{quote(device.account_id, safe='')}/lamps/"
            f"{quote(device.serial, safe='')}/{action}"
        )
        await self.device_mutation(device, "PUT", LAMPS, path)

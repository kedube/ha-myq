"""MyQ remote locks; camera records remain separate camera entities."""

from typing import Any

from homeassistant.components.lock import LockEntity

from .api.locks import Locks, has_remote_control, has_remote_path, lock_online, lock_state
from .entity import MyQEntity, discover_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    discover_entities(
        entry.runtime_data,
        async_add_entities,
        # A Bluetooth-only lock can never be commanded from HA; its sensors remain.
        lambda device: [MyQLock(entry.runtime_data, device)] if has_remote_path(device) else [],
    )


class MyQLock(MyQEntity, LockEntity):
    _attr_name = None

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "lock")
        self.locks = Locks(coordinator.client)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.device is not None
            and has_remote_control(self.device)
            and lock_online(self.device)
        )

    @property
    def is_locked(self) -> bool | None:
        state = lock_state(self.device)
        return None if state is None else state == "locked"

    @property
    def is_locking(self) -> bool | None:
        state = lock_state(self.device)
        return None if state is None else state == "locking"

    @property
    def is_unlocking(self) -> bool | None:
        state = lock_state(self.device)
        return None if state is None else state == "unlocking"

    async def async_lock(self, **kwargs: Any) -> None:
        await self.coordinator.execute(
            self.locks.command(self.device, "lock"), expect=(self.device_key, "locked")
        )

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.coordinator.execute(
            self.locks.command(self.device, "unlock"), expect=(self.device_key, "unlocked")
        )

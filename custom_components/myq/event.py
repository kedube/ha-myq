"""Device history as events: who opened, closed, locked or switched a device, and when.

Disabled by default. History is read (one request per account per full poll) only
while at least one of these entities is enabled.
"""

from homeassistant.components.event import EventEntity
from homeassistant.core import callback

from .api.history import DEVICE_EVENTS, HistoryEvent
from .api.locks import is_lock
from .entity import MyQEntity, discover_entities

PARALLEL_UPDATES = 0
SEEN_LIMIT = 500


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    discover_entities(
        entry.runtime_data,
        async_add_entities,
        lambda device: (
            [MyQHistoryEvent(entry.runtime_data, device)]
            if device.is_door or device.is_lamp or is_lock(device)
            else []
        ),
    )


class MyQHistoryEvent(MyQEntity, EventEntity):
    _attr_translation_key = "activity"
    _attr_icon = "mdi:history"
    _attr_event_types = [*DEVICE_EVENTS, "other"]
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device, "history")
        self._seen: dict[str, None] = {}
        self._primed = False

    @property
    def available(self) -> bool:
        # The record is still meaningful while the device is offline.
        return self.coordinator.last_update_success and self.device is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.follow_history(self.device_key, True)
        self.async_on_remove(lambda: self.coordinator.follow_history(self.device_key, False))
        self._handle_events(fire=False)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._handle_events(fire=True)
        super()._handle_coordinator_update()

    @callback
    def _handle_events(self, *, fire: bool) -> None:
        events: tuple[HistoryEvent, ...] | None = self.coordinator.data.history.get(self.device_key)
        if events is None:
            return
        # The first page read only primes what was already there; later reads fire
        # what is new, oldest first.
        for event in events:
            if event.id in self._seen:
                continue
            self._seen[event.id] = None
            if fire and self._primed:
                details = {
                    key: value
                    for key, value in event.as_dict().items()
                    if key not in {"id", "device_serial", "device_event", "event_type"}
                    and value is not None
                }
                # HA reserves event_type for the device event; the history kind is
                # device_action, key_press, schedule_action, and so on.
                clip = self.coordinator.data.history_clips.get(event.id)
                if clip is not None:
                    # The opener camera's door-triggered clip, playable in the media browser.
                    details["recording"] = clip
                self._trigger_event(event.device_event, {"activity": event.event_type, **details})
                # Each history entry is its own state change, so none is collapsed.
                self.async_write_ha_state()
        self._primed = True
        while len(self._seen) > SEEN_LIMIT:
            self._seen.pop(next(iter(self._seen)))

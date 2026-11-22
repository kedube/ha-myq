"""Share cloud polling across all entities; camera outages do not hide doors."""

import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api.account_access import AccountAccess
from .api.alerts import AlertRule, Alerts
from .api.associations import camera_opener, linked_camera
from .api.camera_commands import CameraCommands
from .api.camera_settings import CameraSettings
from .api.client import Client
from .api.climate import Thermostats
from .api.errors import (
    AccessDenied,
    AuthenticationError,
    CommandRejected,
    MyQError,
    ProtocolError,
    RateLimited,
    SessionRenewalFailed,
)
from .api.garage import GarageSettings, can_configure_garage
from .api.history import History, HistoryEvent, parse_time
from .api.locks import is_lock, lock_state
from .api.management import Accessory, Management
from .api.models import Device
from .api.recordings import Recordings
from .api.status import CameraStatusMonitor
from .api.tend import _alias
from .api.video import Video
from .const import (
    CONF_ENABLE_CAMERAS,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_POLL_INTERVAL,
)
from .exceptions import myq_error, translation

_LOGGER = logging.getLogger(__name__)

# MyQ Android 5.324.0 (TimeoutConfiguration, DeviceTimeoutHandler) follows a commanded
# or moving device until it reports its target state, within a per-type window. The app
# re-reads one account's devices every 1-2 s while on screen; HA keeps a gentler 5 s.
FAST_POLL_SECONDS = 5
# t60/i0.e0: the app re-reads a thermostat two seconds after a change is accepted.
SETTLE_SECONDS = 2.0
DEFAULT_TRACK_SECONDS = 60  # Garage door openers, lamps and other devices.
LOCK_TRACK_SECONDS = 30
TRACK_SECONDS = {"virtualgaragedooropener": 90, "gate": 120, "commercialdooropener": 120}
# The app reads roles, alert rules and camera details only when a screen opens, not on
# a timer. HA re-reads them at these intervals, and sooner after any of its own writes.
ACCESS_TTL = 1800  # Roles gate entity availability only; every write re-reads them.
ACCESSORY_TTL = 300  # Only while an accessory battery sensor is enabled (off by default).
ALERTS_TTL = 1800
CAMERA_TTL = 900  # Status pushes re-read a camera when it reports a change.
CAMERA_PUSH_COOLDOWN = 60
# The app fetches the accounts list when it starts or the account changes (fn0/a), not
# on every poll; a failed device read fetches it again.
ACCOUNTS_TTL = 6 * 3600
MISSING_POLLS_BEFORE_REMOVAL = 3
# Errors that prove MyQ did not act on a command: refused, unauthorized or never sent.
NOT_EXECUTED = (CommandRejected, AccessDenied, SessionRenewalFailed)
_TARGETS = {"opening": "open", "closing": "closed", "locking": "locked", "unlocking": "unlocked"}


def transition(device: Device) -> str | None:
    """The reported in-progress movement of a door or lock, if any."""
    # Check both: a lock's door sensor reports open/closed beside its lock state.
    for state in (device.door_state, lock_state(device)):
        if state in _TARGETS:
            return state
    return None


def reported_states(device: Device) -> set[str]:
    lamp = device.state.get("lamp_state")
    values = (
        device.door_state,
        lock_state(device),
        lamp.lower() if isinstance(lamp, str) else None,
    )
    return {value for value in values if value is not None}


def track_seconds(device: Device) -> int:
    """The app's tracking window for this device type."""
    if device.device_type in TRACK_SECONDS:
        return TRACK_SECONDS[device.device_type]
    if not device.is_door and (is_lock(device) or device.family == "locks"):
        return LOCK_TRACK_SECONDS
    return DEFAULT_TRACK_SECONDS


@dataclass(frozen=True)
class _Tracked:
    target: str
    until: float


@dataclass
class Snapshot:
    devices: dict[str, Device]
    cameras: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    camera_errors: dict[str, str] = field(default_factory=dict)
    garage_settings: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    garage_errors: dict[str, str] = field(default_factory=dict)
    account_access: dict[str, AccountAccess] = field(default_factory=dict, repr=False)
    account_access_error: str | None = None
    accessories: dict[str, tuple[Accessory, ...]] = field(default_factory=dict, repr=False)
    accessory_errors: dict[str, str] = field(default_factory=dict)
    # Newest first page of device history, by device key (only for followed devices).
    history: dict[str, tuple[HistoryEvent, ...]] = field(default_factory=dict, repr=False)
    history_errors: dict[str, str] = field(default_factory=dict)
    # History entry ID -> media-source ID of its door-linked camera clip (tu/j).
    history_clips: dict[str, str] = field(default_factory=dict, repr=False)
    alerts: dict[str, tuple[AlertRule, ...]] = field(default_factory=dict, repr=False)
    alert_errors: dict[str, str] = field(default_factory=dict)


class MyQCoordinator(DataUpdateCoordinator[Snapshot]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: Client) -> None:
        self.poll_interval = timedelta(
            # Older entries may hold the former 30-second minimum.
            seconds=max(
                MIN_POLL_INTERVAL, entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            )
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=self.poll_interval,
        )
        self.client = client
        self.video = Video(client)
        self.camera_settings = CameraSettings(self.video)
        self.thermostats = Thermostats(client)
        self.management = Management(client, self.video, entry.unique_id)
        self.garage = GarageSettings(
            client,
            lambda account_id: self.management.require_access(
                account_id, "devices", "Manage", owner=True
            ),
        )
        self.recordings = Recordings(client)
        self.history = History(client)
        self.alerts = Alerts(client)
        self._history_devices: set[str] = set()
        self._alerts_cache: dict[str, tuple[float, tuple[AlertRule, ...]]] = {}
        self.live_sessions: dict[str, Any] = {}
        self._tracked_accessories: set[str] = set()
        self._accessory_cache: dict[str, tuple[float, tuple[Accessory, ...]]] = {}
        self._access_cache: tuple[float, dict[str, AccountAccess]] | None = None
        self.status_monitor = CameraStatusMonitor(client, self._status_changed, self._status_error)
        self.camera_commands = CameraCommands(
            client, self.status_monitor, lambda key: self.live_sessions.get(key)
        )
        self._status_started = False
        self._live_owners: set[object] = set()
        self._known_aliases: dict[str, str] = {}
        self._camera_refreshers: dict[str, Debouncer] = {}
        self._tracking: dict[str, _Tracked] = {}
        self._settle_unsub: Callable[[], None] | None = None
        # Monotonic times, which count from boot: "never" must be -inf, not 0.0, or a
        # host up for less than a TTL would treat data it never read as fresh.
        self._camera_read_at: dict[str, float] = {}
        self._accounts_read_at = -math.inf
        self._last_full = -math.inf
        self._transitions: set[tuple[str, str]] = set()
        self._missing: dict[str, int] = {}
        self.loaded_options = dict(entry.options)

    @property
    def cameras_enabled(self) -> bool:
        return self.loaded_options.get(CONF_ENABLE_CAMERAS, True)

    async def _async_update_data(self) -> Snapshot:
        now = time.monotonic()
        try:
            accounts = self._tracked_accounts()
            if (
                self.data is not None
                and accounts
                and now - self._last_full < self.poll_interval.total_seconds()
            ):
                snapshot = await self._light_update(self.data, accounts)
            else:
                snapshot = await self._full_update()
                self._last_full = now
        except BaseException:
            # Never keep polling quickly into an outage, quota or reauthentication.
            self._stop_tracking()
            raise
        self._adjust_poll_rate(snapshot)
        return snapshot

    def _auth_failed(self, err: AuthenticationError) -> ConfigEntryAuthFailed:
        self.status_monitor.update_aliases(set())
        return ConfigEntryAuthFailed(**translation(err))

    async def _cloud[T](self, read: Awaitable[T]) -> T:
        try:
            return await read
        except AuthenticationError as err:
            raise self._auth_failed(err) from err
        except RateLimited as err:
            raise UpdateFailed(retry_after=err.retry_after, **translation(err)) from err
        except MyQError as err:
            raise UpdateFailed(**translation(err)) from err

    def _tracked_accounts(self) -> list[str]:
        if self.data is None:
            return []
        devices = self.data.devices
        return sorted({devices[key].account_id for key in self._tracking if key in devices})

    async def _light_update(self, previous: Snapshot, accounts: list[str]) -> Snapshot:
        """Re-read only the tracked devices' accounts, like the app's one-account poll.

        Settings, grants, camera details and other accounts' devices carry over.
        """
        devices = {
            key: device
            for key, device in previous.devices.items()
            if device.account_id not in accounts
        }
        for account_id in accounts:
            for device in await self._cloud(self.client.devices(account_id)):
                devices[device.key] = device

        def present(values: dict) -> dict:
            return {key: value for key, value in values.items() if key in devices}

        return replace(
            previous,
            devices=devices,
            cameras=present(previous.cameras),
            camera_errors=present(previous.camera_errors),
            garage_settings=present(previous.garage_settings),
            garage_errors=present(previous.garage_errors),
            accessories=present(previous.accessories),
            accessory_errors=present(previous.accessory_errors),
            history=present(previous.history),
            history_clips=previous.history_clips,
            alerts=present(previous.alerts),
        )

    async def _full_update(self) -> Snapshot:
        reuse = time.monotonic() - self._accounts_read_at < ACCOUNTS_TTL
        try:
            devices = await self._cloud(self.client.discover(reuse_accounts=reuse))
        except Exception:
            self._accounts_read_at = -math.inf  # The account may have changed; fetch it again.
            raise
        if not reuse:
            self._accounts_read_at = time.monotonic()
        previous = self.data.cameras if self.data is not None else {}
        snapshot = Snapshot(devices)
        await self._read_account_access(snapshot)
        garage_limited = False
        for device in snapshot.devices.values():
            # The app shows opener settings to the owner only; others cost no reads.
            if not can_configure_garage(device, snapshot.account_access.get(device.account_id)):
                continue
            if garage_limited:
                snapshot.garage_errors[device.key] = "RateLimited"
                continue
            try:
                settings = await self.garage.read(device)
                if settings:
                    snapshot.garage_settings[device.key] = settings
            except AuthenticationError as err:
                raise self._auth_failed(err) from err
            except RateLimited:
                # Optional settings must not discard newly discovered device state.
                # Client/transport preserve this origin's Retry-After deadline.
                garage_limited = True
                snapshot.garage_errors[device.key] = "RateLimited"
            except MyQError as err:
                snapshot.garage_errors[device.key] = type(err).__name__
        if self.cameras_enabled:
            camera_limited = False
            for device in snapshot.devices.values():
                if not device.is_camera or not device.online:
                    continue
                if camera_limited:
                    snapshot.camera_errors[device.key] = "RateLimited"
                    continue
                read_at = self._camera_read_at.get(device.key, -math.inf)
                if device.key in previous and time.monotonic() - read_at < CAMERA_TTL:
                    snapshot.cameras[device.key] = previous[device.key]
                    continue
                try:
                    snapshot.cameras[device.key] = await self.video.details(device)
                    self._camera_read_at[device.key] = time.monotonic()
                except AuthenticationError as err:
                    raise self._auth_failed(err) from err
                except RateLimited:
                    camera_limited = True
                    snapshot.camera_errors[device.key] = "RateLimited"
                except MyQError as err:
                    snapshot.camera_errors[device.key] = type(err).__name__
        await self._read_accessories(snapshot)
        await self._read_history(snapshot)
        await self._read_alerts(snapshot)
        self._update_status_aliases(snapshot)
        self._prune_devices(snapshot)
        return snapshot

    async def _read_account_access(self, snapshot: Snapshot) -> None:
        cached = self._access_cache
        if cached is not None and time.monotonic() - cached[0] < ACCESS_TTL:
            snapshot.account_access = cached[1]
            return
        try:
            access = await self.management.account_access()
        except AuthenticationError as err:
            raise self._auth_failed(err) from err
        except MyQError as err:
            # Without AccountAccess only ownership is known (from created_by), as in the
            # app; observed states are kept and stale roles are not reused.
            self._access_cache = None
            snapshot.account_access_error = type(err).__name__
            snapshot.account_access = self.management.owner_access()
            return
        self._access_cache = time.monotonic(), access
        snapshot.account_access = access

    async def _read_accessories(self, snapshot: Snapshot) -> None:
        for key in self._tracked_accessories & snapshot.devices.keys():
            device = snapshot.devices[key]
            access = snapshot.account_access.get(device.account_id)
            # The Remotes & Keypads screen is the owner's (y80/o).
            if not device.online or access is None or not access.owner:
                self._accessory_cache.pop(key, None)
                continue
            cached = self._accessory_cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < ACCESSORY_TTL:
                snapshot.accessories[key] = cached[1]
                continue
            try:
                items = await self.management.accessories(device)
                self._accessory_cache[key] = time.monotonic(), items
                snapshot.accessories[key] = items
            except AuthenticationError as err:
                raise self._auth_failed(err) from err
            except MyQError as err:
                self._accessory_cache.pop(key, None)
                snapshot.accessory_errors[key] = type(err).__name__
        for key in self._accessory_cache.keys() - snapshot.devices.keys():
            self._accessory_cache.pop(key, None)

    def _optional_accounts(self, snapshot: Snapshot, resource: str) -> dict[str, list[Device]]:
        accounts: dict[str, list[Device]] = {}
        for device in snapshot.devices.values():
            access = snapshot.account_access.get(device.account_id)
            if access is not None and access.allows(resource, "View"):
                accounts.setdefault(device.account_id, []).append(device)
        return accounts

    async def _read_history(self, snapshot: Snapshot) -> None:
        """One history page per account, only while a history event entity is enabled."""
        followed = self._history_devices & snapshot.devices.keys()
        for account_id, devices in self._optional_accounts(snapshot, "history").items():
            wanted = [device for device in devices if device.key in followed]
            if not wanted:
                continue
            try:
                page = await self.history.read(account_id, [device.serial for device in wanted])
            except AuthenticationError as err:
                raise self._auth_failed(err) from err
            except RateLimited:
                snapshot.history_errors[account_id] = "RateLimited"
                return
            except MyQError as err:
                snapshot.history_errors[account_id] = type(err).__name__
                continue
            for device in wanted:
                snapshot.history[device.key] = tuple(
                    event for event in page.events if event.device_serial == device.serial
                )
            if await self._link_door_clips(snapshot, wanted) is False:
                return

    async def door_clip_links(
        self, door: Device, events: tuple[HistoryEvent, ...] | list[HistoryEvent]
    ) -> dict[str, str]:
        """Match door history to the opener camera's door-triggered clips, as the app's
        history screen does (tu/j: accessEventID.GD_OPEN/GD_CLOSE equals the entry time).

        One clip read for the window; returns history entry ID -> media-source ID.
        """
        if not self.cameras_enabled or self.data is None:
            return {}
        from .media_source import recording_content_id

        doors = [event for event in events if event.device_event in {"open", "closed"}]
        camera = linked_camera(door, self.data.devices) if door.is_door else None
        if not doors or camera is None or not camera.online:
            return {}
        start = min(event.timestamp for event in doors) - timedelta(seconds=5)
        clips = await self.recordings.door_clips(
            camera, start.isoformat(), dt_util.utcnow().isoformat()
        )
        times: dict = {}
        for clip in clips:
            if (moment := parse_time(clip.door_time)) is not None:
                times.setdefault(moment, clip.id)
        links = {}
        for event in doors:
            match = times.get(event.timestamp) or next(
                (
                    clip_id
                    for moment, clip_id in times.items()
                    if abs((moment - event.timestamp).total_seconds()) <= 2
                ),
                None,
            )
            if match is not None:
                links[event.id] = recording_content_id(self.config_entry.entry_id, camera, match)
        return links

    async def _link_door_clips(self, snapshot: Snapshot, doors: list[Device]) -> bool:
        """One clip read per followed opener with a camera. False stops on a rate limit."""
        for door in doors:
            try:
                snapshot.history_clips.update(
                    await self.door_clip_links(door, snapshot.history.get(door.key, ()))
                )
            except AuthenticationError as err:
                raise self._auth_failed(err) from err
            except RateLimited:
                return False
            except MyQError:
                continue
        return True

    async def _read_alerts(self, snapshot: Snapshot) -> None:
        accounts = self._optional_accounts(snapshot, "alerts")
        for account_id in self._alerts_cache.keys() - accounts.keys():
            self._alerts_cache.pop(account_id, None)
        for account_id, devices in accounts.items():
            cached = self._alerts_cache.get(account_id)
            if cached is not None and time.monotonic() - cached[0] < ALERTS_TTL:
                rules = cached[1]
            else:
                try:
                    rules = await self.alerts.rules(account_id)
                except AuthenticationError as err:
                    raise self._auth_failed(err) from err
                except RateLimited:
                    snapshot.alert_errors[account_id] = "RateLimited"
                    return
                except MyQError as err:
                    self._alerts_cache.pop(account_id, None)
                    snapshot.alert_errors[account_id] = type(err).__name__
                    continue
                self._alerts_cache[account_id] = time.monotonic(), rules
            for device in devices:
                found = tuple(rule for rule in rules if rule.serial == device.serial)
                if found:
                    snapshot.alerts[device.key] = found

    @callback
    def link_camera_devices(self) -> None:
        """Show each opener camera as part of its opener (Device.u attached camera)."""
        if self.data is None:
            return
        registry = dr.async_get(self.hass)
        for camera in self.data.devices.values():
            if not camera.is_camera:
                continue
            opener = camera_opener(camera, self.data.devices)
            camera_entry = registry.async_get_device(identifiers={(DOMAIN, camera.key)})
            if opener is None or camera_entry is None:
                continue
            opener_entry = registry.async_get_device(identifiers={(DOMAIN, opener.key)})
            if opener_entry is not None and camera_entry.via_device_id != opener_entry.id:
                registry.async_update_device(camera_entry.id, via_device_id=opener_entry.id)

    @callback
    def follow_history(self, key: str, enabled: bool) -> None:
        """Called by history event entities as they are added and removed."""
        if enabled:
            self._history_devices.add(key)
        else:
            self._history_devices.discard(key)

    async def async_set_alert(self, device: Device, rule_id: str, enabled: bool) -> None:
        await self.management.require_access(device.account_id, "alerts", "Manage")
        rule = await self.alerts.rule(device, rule_id)
        await self.alerts.set_enabled(device, rule, enabled)
        self._alerts_cache.pop(device.account_id, None)

        async def confirmed() -> bool:
            return (await self.alerts.rule(device, rule_id)).enabled is enabled

        await self.management._confirm(confirmed)

    @callback
    def _prune_devices(self, snapshot: Snapshot) -> None:
        """Remove registry devices MyQ has stopped returning for several full polls."""
        if not snapshot.devices:
            return  # An empty inventory is more likely a cloud fault than a removal.
        registry = dr.async_get(self.hass)
        entries = dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
        for device_entry in entries:
            keys = {key for domain, key in device_entry.identifiers if domain == DOMAIN}
            if not keys or keys & snapshot.devices.keys():
                self._missing.pop(device_entry.id, None)
                continue
            misses = self._missing.get(device_entry.id, 0) + 1
            if misses < MISSING_POLLS_BEFORE_REMOVAL:
                self._missing[device_entry.id] = misses
                continue
            self._missing.pop(device_entry.id, None)
            registry.async_update_device(
                device_entry.id, remove_config_entry_id=self.config_entry.entry_id
            )

    @callback
    def _adjust_poll_rate(self, snapshot: Snapshot) -> None:
        now = time.monotonic()
        moving = {
            (key, state)
            for key, device in snapshot.devices.items()
            if (state := transition(device)) is not None
        }
        # Movement started elsewhere (wall button, app) is followed like a command, as
        # the app does. Unlike the app, a state stuck in "opening" is the same pair, so
        # it cannot re-arm tracking after its window ends.
        for key, state in moving - self._transitions:
            if key not in self._tracking:
                window = track_seconds(snapshot.devices[key])
                self._tracking[key] = _Tracked(_TARGETS[state], now + window)
        self._transitions = moving
        for key, tracked in list(self._tracking.items()):
            device = snapshot.devices.get(key)
            if device is None or now >= tracked.until or tracked.target in reported_states(device):
                del self._tracking[key]
        self._set_poll_rate()

    @callback
    def track(self, key: str, target: str) -> None:
        """Re-read a commanded device quickly until it reports target (or its window ends)."""
        device = self.data.devices.get(key) if self.data else None
        if device is None:
            return
        self._tracking[key] = _Tracked(target, time.monotonic() + track_seconds(device))
        self._set_poll_rate()

    @callback
    def _stop_tracking(self) -> None:
        self._tracking.clear()
        self._set_poll_rate()

    @callback
    def _set_poll_rate(self) -> None:
        fast = timedelta(seconds=FAST_POLL_SECONDS)
        self.update_interval = fast if self._tracking else self.poll_interval

    def _update_status_aliases(self, snapshot: Snapshot) -> None:
        camera_keys = {key for key, device in snapshot.devices.items() if device.is_camera}
        for key in self._known_aliases.keys() - camera_keys:
            self._known_aliases.pop(key, None)
        for key, details in snapshot.cameras.items():
            try:
                self._known_aliases[key] = _alias(details.get("alias"))
            except ProtocolError:
                self._known_aliases.pop(key, None)
        aliases = {
            alias
            for key, alias in self._known_aliases.items()
            if (access := snapshot.account_access.get(snapshot.devices[key].account_id)) is not None
            and access.allows("devices", "View")
        }
        self.status_monitor.update_aliases(
            aliases if self.cameras_enabled and len(aliases) <= 4096 else set()
        )

    @callback
    def _status_changed(self, alias: str) -> None:
        if not self._status_started or self._live_owners:
            return
        for key, known in self._known_aliases.items():
            if known == alias:
                self.request_camera_refresh(key)

    @callback
    def request_camera_refresh(self, key: str) -> None:
        """Re-read one camera after a status push instead of polling the whole account."""
        refresher = self._camera_refreshers.get(key)
        if refresher is None:
            refresher = self._camera_refreshers[key] = Debouncer(
                self.hass,
                _LOGGER,
                cooldown=CAMERA_PUSH_COOLDOWN,
                immediate=True,
                function=partial(self._refresh_camera, key),
                background=True,
            )
        refresher.async_schedule_call()

    async def _refresh_camera(self, key: str) -> None:
        device = self.data.devices.get(key) if self.data is not None else None
        if device is None or not device.is_camera or not device.online or not self.cameras_enabled:
            return
        try:
            details = await self.video.details(device)
            self._camera_read_at[key] = time.monotonic()
        except AuthenticationError:
            self.status_monitor.update_aliases(set())
            self.config_entry.async_start_reauth(self.hass)
            return
        except MyQError as err:
            # Supplemental only; the next full poll records camera errors.
            _LOGGER.debug("Camera status refresh failed (%s)", type(err).__name__)
            return
        current = self.data
        if current is None or key not in current.devices:
            return
        snapshot = replace(
            current,
            cameras={**current.cameras, key: details},
            camera_errors={k: v for k, v in current.camera_errors.items() if k != key},
        )
        self._update_status_aliases(snapshot)
        # Not async_set_updated_data(): that would also postpone the next full poll,
        # so a chatty camera could keep door and lock states from ever refreshing.
        self.data = snapshot
        self.async_update_listeners()

    @callback
    def _stop_camera_refreshes(self) -> None:
        for refresher in self._camera_refreshers.values():
            refresher.async_shutdown()
        self._camera_refreshers.clear()

    async def _status_error(self, error: MyQError) -> None:
        if isinstance(error, AuthenticationError) and self._status_started:
            self.config_entry.async_start_reauth(self.hass)
        # Status notifications are supplemental; polling retains error handling.
        _LOGGER.debug("Camera status connection stopped (%s)", type(error).__name__)

    async def async_start_status(self) -> None:
        self._status_started = True
        if not self._live_owners:
            await self.status_monitor.start()

    async def async_suspend_status(self) -> None:
        self._status_started = False
        await self.status_monitor.pause()
        self._stop_camera_refreshes()

    async def async_shutdown(self) -> None:
        if self._settle_unsub is not None:
            self._settle_unsub()
            self._settle_unsub = None
        self._stop_camera_refreshes()
        await self.camera_commands.close()
        await super().async_shutdown()
        await self.client.close()

    async def async_acquire_live(self, owner: object) -> None:
        self._live_owners.add(owner)
        try:
            await self.status_monitor.pause()
        except BaseException:
            self._live_owners.discard(owner)
            raise

    async def async_release_live(self, owner: object) -> None:
        self._live_owners.discard(owner)
        if self._status_started and not self._live_owners:
            await self.status_monitor.resume()

    @callback
    def track_registered_accessories(self) -> None:
        """Resume accessory battery polling for sensors still enabled in the registry."""
        registry = er.async_get(self.hass)
        for entity in er.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            key, marker, rest = entity.unique_id.partition(":accessory_")
            if marker and rest.endswith("_battery") and entity.disabled_by is None:
                self._tracked_accessories.add(key)

    async def async_refresh_accessories(self, device: Device) -> tuple[Accessory, ...]:
        await self.management.require_access(device.account_id, "devices", "View", owner=True)
        items = await self.management.accessories(device)
        self._tracked_accessories.add(device.key)
        self._accessory_cache[device.key] = time.monotonic(), items
        return items

    def invalidate_accessories(self, device: Device) -> None:
        self._accessory_cache.pop(device.key, None)

    async def execute(
        self,
        operation: Awaitable[Any],
        *,
        expect: tuple[str, str] | None = None,
        settle: float | None = None,
    ) -> Any:
        """Run one cloud operation; expect=(device key, target state) follows a command.

        settle re-reads once after that many seconds instead of straight away, for
        devices whose readback lags an accepted change.
        """
        # A command result, or a rejection, can follow a permission change, and a camera
        # setting change shows only in fresh camera details.
        self._access_cache = None
        self._camera_read_at.clear()
        try:
            result = await operation
        except AuthenticationError as err:
            # Raised only when MyQ ended the session at the token endpoint; a 401 on
            # the command itself was already renewed and resent once by the client.
            self.status_monitor.update_aliases(set())
            self.config_entry.async_start_reauth(self.hass)
            raise myq_error(err) from err
        except MyQError as err:
            # A timeout is not proof of failure. Refresh state, never repeat a command.
            if expect and not isinstance(err, NOT_EXECUTED):
                self.track(*expect)  # The command may still have been accepted.
            await self.async_request_refresh()
            raise myq_error(err) from err
        if expect:
            self.track(*expect)
        if settle:
            self._refresh_later(settle)
        else:
            await self.async_request_refresh()
        return result

    @callback
    def _refresh_later(self, delay: float) -> None:
        if self._settle_unsub is not None:
            self._settle_unsub()
        self._settle_unsub = async_call_later(self.hass, delay, self._settled)

    async def _settled(self, _now: Any) -> None:
        self._settle_unsub = None
        await self.async_request_refresh()

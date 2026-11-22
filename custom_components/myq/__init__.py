"""Set up the MyQ integration."""

import asyncio
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api.backoff import ServiceBackoff
from .api.client import Client
from .api.const import VERSION
from .api.errors import MyQError
from .api.tokens import Tokens
from .const import CONF_TOKENS, DOMAIN, PANEL_OPTION_KEYS, PLATFORMS
from .coordinator import MyQCoordinator
from .panel import async_setup_panel
from .services import async_register_services

type MyQConfigEntry = ConfigEntry[MyQCoordinator]

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
REVOKE_TIMEOUT = 15
CARD_FILE = Path(__file__).parent / "www" / "myq-live-card.js"
# Manually added dashboard resources keep working; this path is never cached.
LEGACY_CARD_URL = "/myq/live-card.js"
# Versioned, so browsers may cache it until the next release changes the path.
CARD_URL = f"/myq/{VERSION}/live-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(LEGACY_CARD_URL, str(CARD_FILE), cache_headers=False),
            StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=True),
        ]
    )
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, CARD_URL)
    # Registered once, independent of entries; handlers require a loaded entry.
    async_register_services(hass)
    await async_setup_panel(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MyQConfigEntry) -> bool:
    if not isinstance(entry.data.get(CONF_TOKENS), dict):
        # Home Assistant's own MyQ integration, removed in 2023, used this domain too. Its
        # leftover entries hold a username and password, which MyQ no longer accepts.
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="legacy_entry")

    async def save_tokens(tokens: Tokens) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TOKENS: tokens.as_dict(),
            },
        )

    # One cooldown store per entry for the life of Home Assistant, so a setup retry or
    # reload cannot skip a Retry-After (HA retries setup after 5, 10, 20... seconds).
    backoff = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, ServiceBackoff())
    client = Client(
        async_get_clientsession(hass),
        Tokens(**entry.data[CONF_TOKENS]),
        save_tokens,
        backoff=backoff,
    )
    coordinator = MyQCoordinator(hass, entry, client)
    entry.async_on_unload(client.close)
    coordinator.track_registered_accessories()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.merge_gateway_devices()
    # Register first so newly discovered entities use the merged device immediately.
    entry.async_on_unload(coordinator.async_add_listener(coordinator.merge_gateway_devices))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Registry links need both devices registered, so they follow entity setup.
    coordinator.link_camera_devices()
    entry.async_on_unload(coordinator.async_add_listener(coordinator.link_camera_devices))
    await coordinator.async_start_status()
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    return True


async def async_options_updated(hass: HomeAssistant, entry: MyQConfigEntry) -> None:
    # The panel follows entry changes itself. Only backend options require a reload.
    current = {key: value for key, value in entry.options.items() if key not in PANEL_OPTION_KEYS}
    loaded = {
        key: value
        for key, value in entry.runtime_data.loaded_options.items()
        if key not in PANEL_OPTION_KEYS
    }
    if current != loaded:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MyQConfigEntry) -> bool:
    # A timed siren started without a live view ends with an explicit stop.
    await entry.runtime_data.camera_commands.close()
    await entry.runtime_data.async_suspend_status()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.status_monitor.close()
    else:
        await entry.runtime_data.async_start_status()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: MyQConfigEntry) -> None:
    """End this integration's MyQ session, as the app's sign-out revokes its own.

    Best effort and bounded: removal never waits on, or fails because of, MyQ. Only
    this entry's session ends; the phone app and other sign-ins are unaffected.
    """
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    stored = entry.data.get(CONF_TOKENS)
    if not isinstance(stored, dict):
        return
    try:
        tokens = Tokens(**stored)
    except TypeError:
        return
    # No token-update callback: the entry is gone, so a rotation is not saved.
    client = Client(async_get_clientsession(hass), tokens)
    try:
        async with asyncio.timeout(REVOKE_TIMEOUT):
            revoked = await client.auth.revoke()
    except (MyQError, TimeoutError) as err:
        _LOGGER.debug("MyQ session revocation failed (%s)", type(err).__name__)
        return
    finally:
        await client.close()
    _LOGGER.debug("MyQ session revocation %s", "confirmed" if revoked else "not confirmed")


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: MyQConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device only once MyQ no longer returns it."""
    current = entry.runtime_data.data.devices
    return not any(domain == DOMAIN and key in current for domain, key in device.identifiers)

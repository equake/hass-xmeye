"""XMEye/Sofia integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONFIG_ENTRY_VERSION
from .coordinator import XMEyeCoordinator

_LOGGER = logging.getLogger(__name__)

type XMEyeConfigEntry = ConfigEntry[XMEyeCoordinator]

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: XMEyeConfigEntry) -> bool:
    coordinator = XMEyeCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: XMEyeConfigEntry) -> None:
    """Reload the entry when options change (e.g. storage refresh interval)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: XMEyeConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: XMEyeCoordinator = entry.runtime_data
        await coordinator.async_shutdown()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: XMEyeConfigEntry) -> bool:
    """Migrate config entry to the current version."""
    _LOGGER.debug("Migrating XMEye entry from version %s", entry.version)
    if entry.version == CONFIG_ENTRY_VERSION:
        return True
    _LOGGER.error("Cannot migrate XMEye config entry from version %s", entry.version)
    return False

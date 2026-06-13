"""Diagnostics support for XMEye/Sofia integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import XMEyeConfigEntry

TO_REDACT = {CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "connection": {
            "connected": coordinator.connected,
            "channel_count": coordinator.channel_count,
            "device_type": coordinator.device_type,
        },
        "device_info_cache": coordinator.device_info_cache,
        "storage_cache": coordinator.storage_cache,
        "alarm_states": {
            f"ch{ch}_{et}": v for (ch, et), v in coordinator.states.items()
        },
    }

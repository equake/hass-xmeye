"""Switches to enable/disable recording and motion detection per channel."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .coordinator import XMEyeCoordinator

_LOGGER = logging.getLogger(__name__)

_POLL_INTERVAL = 60  # seconds between state re-sync polls


@dataclass(frozen=True, kw_only=True)
class XMEyeSwitchDescription(SwitchEntityDescription):
    kind: str  # "motion" | "recording"


_SWITCH_TYPES: tuple[XMEyeSwitchDescription, ...] = (
    XMEyeSwitchDescription(
        key="motion_detection",
        translation_key="motion_detection",
        kind="motion",
        icon="mdi:motion-sensor",
    ),
    XMEyeSwitchDescription(
        key="recording",
        translation_key="recording",
        kind="recording",
        icon="mdi:record-circle",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XMEyeCoordinator = entry.runtime_data
    async_add_entities(
        XMEyeSwitch(coordinator, channel, description)
        for channel in range(coordinator.channel_count)
        for description in _SWITCH_TYPES
    )


class XMEyeSwitch(SwitchEntity):
    """Switch that enables or disables a per-channel feature on an XMEye device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: XMEyeCoordinator,
        channel: int,
        description: XMEyeSwitchDescription,
    ) -> None:
        self._coordinator = coordinator
        self._channel = channel
        self._description = description
        self.entity_description = description
        self._remove_listener: Callable[[], None] | None = None
        self._is_on: bool | None = None  # None = unknown until first read

        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_ch{channel}_{description.key}"
        )

    @property
    def name(self) -> str:
        label = self._description.translation_key.replace("_", " ").title()
        return f"CH{self._channel + 1} {label}"

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def available(self) -> bool:
        return self._coordinator.connected

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        try:
            if self._description.kind == "motion":
                await self._coordinator.async_set_motion_enabled(self._channel, enabled)
            else:
                await self._coordinator.async_set_recording_enabled(self._channel, enabled)
            self._is_on = enabled
            self.async_write_ha_state()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Failed to set %s ch%d to %s: %s",
                self._description.key,
                self._channel + 1,
                enabled,
                err,
            )

    async def async_update(self) -> None:
        """Re-read the actual state from the device (called by HA poll if enabled)."""
        try:
            if self._description.kind == "motion":
                self._is_on = await self._read_motion_state()
            else:
                self._is_on = await self._read_recording_state()
        except Exception:  # noqa: BLE001
            pass

    async def _read_motion_state(self) -> bool | None:
        async def _get(client):
            from .const import CONF_NAME_MOTION
            data = await client.config_get(CONF_NAME_MOTION)
            if not data:
                return None
            entries = data if isinstance(data, list) else [data]
            if self._channel < len(entries):
                return bool(entries[self._channel].get("Enable", True))
            return None
        return await self._coordinator.async_run_command(_get)

    async def _read_recording_state(self) -> bool | None:
        async def _get(client):
            from .const import CONF_NAME_ENCODE, CONF_NAME_ENCODE_ALT
            for name in (CONF_NAME_ENCODE, CONF_NAME_ENCODE_ALT):
                data = await client.config_get(name)
                if data:
                    entries = data if isinstance(data, list) else [data]
                    if self._channel < len(entries):
                        enc = entries[self._channel]
                        if "MainFormat" in enc:
                            return bool(enc["MainFormat"].get("Video", {}).get("Enable", True))
                        if "Video" in enc:
                            return bool(enc["Video"].get("Enable", True))
                        return bool(enc.get("Enable", True))
            return None
        return await self._coordinator.async_run_command(_get)

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(self._handle_update)
        # Read initial state
        await self.async_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

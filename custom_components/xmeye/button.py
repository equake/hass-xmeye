"""Reboot button for XMEye/Sofia devices."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .coordinator import XMEyeCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XMEyeCoordinator = entry.runtime_data
    async_add_entities([XMEyeRebootButton(coordinator)])


class XMEyeRebootButton(ButtonEntity):
    """Button that reboots the DVR/NVR/camera."""

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_translation_key = "reboot"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: XMEyeCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_reboot"

    @property
    def device_info(self):
        return self._coordinator.device_info

    async def async_press(self) -> None:
        await self._coordinator.async_run_command(lambda c: c.reboot())

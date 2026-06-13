"""Base entity class shared by all XMEye/Sofia platforms."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .coordinator import XMEyeCoordinator


class XMEyeEntity(Entity):
    """Common base for every XMEye entity.

    Handles coordinator listener lifecycle, device_info, and availability so
    that individual platform classes only need to implement their own state.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: XMEyeCoordinator) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._remove_listener: Callable[[], None] | None = None

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def available(self) -> bool:
        return self._coordinator.connected

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_listener = self._coordinator.async_add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

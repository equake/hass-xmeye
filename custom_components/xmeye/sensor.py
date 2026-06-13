"""Sensors for XMEye/Sofia device info and storage status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .coordinator import XMEyeCoordinator


@dataclass(frozen=True, kw_only=True)
class XMEyeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[XMEyeCoordinator], object]


_SENSOR_TYPES: tuple[XMEyeSensorDescription, ...] = (
    XMEyeSensorDescription(
        key="firmware",
        translation_key="firmware",
        value_fn=lambda c: c.device_info_cache.get("firmware"),
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    XMEyeSensorDescription(
        key="hdd_total",
        translation_key="hdd_total",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        value_fn=lambda c: c.storage_cache[0]["total_gb"] if c.storage_cache else None,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    XMEyeSensorDescription(
        key="hdd_used",
        translation_key="hdd_used",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        value_fn=lambda c: c.storage_cache[0]["used_gb"] if c.storage_cache else None,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    XMEyeSensorDescription(
        key="hdd_status",
        translation_key="hdd_status",
        value_fn=lambda c: c.storage_cache[0]["status"] if c.storage_cache else None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XMEyeCoordinator = entry.runtime_data
    async_add_entities(
        XMEyeSensorEntity(coordinator, description) for description in _SENSOR_TYPES
    )


class XMEyeSensorEntity(SensorEntity):
    """Sensor showing device-level information from an XMEye device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: XMEyeCoordinator,
        description: XMEyeSensorDescription,
    ) -> None:
        self._coordinator = coordinator
        self._description = description
        self.entity_description = description
        self._remove_listener: Callable[[], None] | None = None
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def native_value(self) -> object:
        try:
            return self._description.value_fn(self._coordinator)
        except Exception:  # noqa: BLE001
            return None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

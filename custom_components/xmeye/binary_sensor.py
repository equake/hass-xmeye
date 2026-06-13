"""Binary sensors for XMEye/Sofia alarm events."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .coordinator import XMEyeCoordinator


@dataclass(frozen=True, kw_only=True)
class XMEyeSensorDescription(BinarySensorEntityDescription):
    event_type: str


_SENSOR_TYPES: tuple[XMEyeSensorDescription, ...] = (
    XMEyeSensorDescription(
        key="motion",
        event_type="MotionDetect",
        translation_key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
    ),
    XMEyeSensorDescription(
        key="video_loss",
        event_type="VideoLost",
        translation_key="video_loss",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    XMEyeSensorDescription(
        key="video_blind",
        event_type="HideAlarm",
        translation_key="video_blind",
        device_class=BinarySensorDeviceClass.TAMPER,
    ),
    XMEyeSensorDescription(
        key="alarm_input",
        event_type="AlarmLocal",
        translation_key="alarm_input",
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
    XMEyeSensorDescription(
        key="io_alarm",
        event_type="IOAlarm",
        translation_key="io_alarm",
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
    XMEyeSensorDescription(
        key="cross_line",
        event_type="CrossLineDetection",
        translation_key="cross_line",
        device_class=BinarySensorDeviceClass.MOTION,
    ),
    XMEyeSensorDescription(
        key="intrusion",
        event_type="PEAAlarm",
        translation_key="intrusion",
        device_class=BinarySensorDeviceClass.MOTION,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XMEyeCoordinator = entry.runtime_data

    entities = [
        XMEyeBinarySensor(coordinator, channel, description)
        for channel in range(coordinator.channel_count)
        for description in _SENSOR_TYPES
    ]
    async_add_entities(entities)


class XMEyeBinarySensor(BinarySensorEntity):
    """Binary sensor representing a single alarm event on one camera channel."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: XMEyeCoordinator,
        channel: int,
        description: XMEyeSensorDescription,
    ) -> None:
        self._coordinator = coordinator
        self._channel = channel
        self._description = description
        self._remove_listener: Callable[[], None] | None = None

        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_{description.key}"
        self.entity_description = description

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def name(self) -> str:
        return f"CH{self._channel + 1} {self._description.translation_key.replace('_', ' ').title()}"

    @property
    def available(self) -> bool:
        return self._coordinator.connected

    @property
    def is_on(self) -> bool:
        return self._coordinator.states.get(
            (self._channel, self._description.event_type), False
        )

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(
            self._handle_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

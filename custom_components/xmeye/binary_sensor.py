"""Binary sensors for XMEye/Sofia alarm events."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .const import SIGNAL_NEW_CHANNEL
from .coordinator import XMEyeCoordinator
from .entity import XMEyeEntity


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
    async_add_entities([
        XMEyeBinarySensor(coordinator, ch, desc)
        for ch in sorted(coordinator.connected_channels)
        for desc in _SENSOR_TYPES
    ])

    def _on_new_channel(channel: int) -> None:
        async_add_entities([
            XMEyeBinarySensor(coordinator, channel, desc)
            for desc in _SENSOR_TYPES
        ])

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_CHANNEL.format(entry.entry_id), _on_new_channel
        )
    )


class XMEyeBinarySensor(XMEyeEntity, BinarySensorEntity):
    """Binary sensor for one alarm event type on one camera channel."""

    def __init__(
        self,
        coordinator: XMEyeCoordinator,
        channel: int,
        description: XMEyeSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._description = description
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_ch{channel}_{description.key}"
        self._attr_translation_placeholders = {"channel": str(channel + 1)}

    @property
    def is_on(self) -> bool:
        return self._coordinator.states.get(
            (self._channel, self._description.event_type), False
        )

"""Per-channel control switches: detection (motion/human/face), recording, privacy."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import XMEyeConfigEntry
from .const import SIGNAL_NEW_CHANNEL
from .coordinator import XMEyeCoordinator
from .entity import XMEyeEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class XMEyeSwitchDescription(SwitchEntityDescription):
    kind: str  # "motion" | "human" | "face" | "recording" | "privacy"


# Detection switches — created only when the device reports support for the kind.
_DETECT_SWITCHES: tuple[XMEyeSwitchDescription, ...] = (
    XMEyeSwitchDescription(
        key="motion_detection", translation_key="motion_detection",
        kind="motion", icon="mdi:motion-sensor",
    ),
    XMEyeSwitchDescription(
        key="human_detection", translation_key="human_detection",
        kind="human", icon="mdi:human",
    ),
    XMEyeSwitchDescription(
        key="face_detection", translation_key="face_detection",
        kind="face", icon="mdi:face-recognition",
    ),
)
_RECORDING = XMEyeSwitchDescription(
    key="recording", translation_key="recording", kind="recording", icon="mdi:record-circle",
)
_PRIVACY = XMEyeSwitchDescription(
    key="privacy", translation_key="privacy", kind="privacy", icon="mdi:eye-off",
)


def _switches_for_channel(
    coordinator: XMEyeCoordinator, channel: int
) -> list[XMEyeSwitch]:
    out = [
        XMEyeSwitch(coordinator, channel, desc)
        for desc in _DETECT_SWITCHES
        if coordinator.detect_supported(desc.kind, channel)
    ]
    out.append(XMEyeSwitch(coordinator, channel, _RECORDING))
    out.append(XMEyeSwitch(coordinator, channel, _PRIVACY))
    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XMEyeCoordinator = entry.runtime_data
    # Populate the control caches so detection switches can be capability-gated.
    try:
        await coordinator.async_refresh_controls()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not pre-fetch controls for switches", exc_info=True)

    entities: list[XMEyeSwitch] = []
    for ch in sorted(coordinator.connected_channels):
        entities.extend(_switches_for_channel(coordinator, ch))
    async_add_entities(entities)

    def _on_new_channel(channel: int) -> None:
        async_add_entities(_switches_for_channel(coordinator, channel))

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_CHANNEL.format(entry.entry_id), _on_new_channel
        )
    )


class XMEyeSwitch(XMEyeEntity, SwitchEntity, RestoreEntity):
    """A per-channel control switch (detection / recording / privacy)."""

    def __init__(
        self,
        coordinator: XMEyeCoordinator,
        channel: int,
        description: XMEyeSwitchDescription,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._description = description
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_ch{channel}_{description.key}"
        self._attr_translation_placeholders = {"channel": str(channel + 1)}

    @property
    def available(self) -> bool:
        if not self._coordinator.connected:
            return False
        kind = self._description.kind
        if kind == "privacy":
            return True  # HA-side state, always actionable while connected
        if kind == "recording":
            return self._coordinator.recording_on(self._channel) is not None
        return self._coordinator.detect_enabled(kind, self._channel) is not None

    @property
    def is_on(self) -> bool | None:
        kind = self._description.kind
        if kind == "privacy":
            return self._channel in self._coordinator.private_channels
        if kind == "recording":
            return self._coordinator.recording_on(self._channel)
        return self._coordinator.detect_enabled(kind, self._channel)

    async def async_turn_on(self, **kwargs) -> None:
        await self._apply(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._apply(False)

    async def _apply(self, on: bool) -> None:
        kind = self._description.kind
        try:
            if kind == "privacy":
                await self._coordinator.async_set_privacy(self._channel, on)
            elif kind == "recording":
                await self._coordinator.async_set_recording_on(self._channel, on)
            else:
                await self._coordinator.async_set_detect_enabled(kind, self._channel, on)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Failed to set %s ch%d to %s: %s",
                self._description.key, self._channel + 1, on, err,
            )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Privacy is HA-side state — restore it across restarts so the camera stays
        # hidden (the device already persists ClosedRecord on its own).
        if self._description.kind == "privacy":
            last = await self.async_get_last_state()
            if last is not None and last.state == "on":
                self._coordinator.private_channels.add(self._channel)
                self.async_write_ha_state()

"""Camera platform for XMEye/Sofia devices — RTSP stream, snapshot and PTZ."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import aiohttp

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .client import sofia_hash
from .coordinator import XMEyeCoordinator

_LOGGER = logging.getLogger(__name__)

# Pan/tilt/zoom direction mapping from HA service values to DVRIP command strings
_PTZ_MAP: dict[tuple[str | None, str | None, str | None], str] = {
    ("UP", None, None): "DirectionUp",
    ("DOWN", None, None): "DirectionDown",
    (None, "LEFT", None): "DirectionLeft",
    (None, "RIGHT", None): "DirectionRight",
    ("UP", "LEFT", None): "DirectionUpLeft",
    ("UP", "RIGHT", None): "DirectionUpRight",
    ("DOWN", "LEFT", None): "DirectionDownLeft",
    ("DOWN", "RIGHT", None): "DirectionDownRight",
    (None, None, "IN"): "ZoomTele",
    (None, None, "OUT"): "ZoomWide",
}

_SNAPSHOT_PATHS = [
    "/web/cgi-bin/hi3510/snapPicture.cgi?chn={channel}",
    "/cgi-bin/snapshot.cgi?chn={channel}&q=0",
    "/snap.jpg?channel={channel}",
]

_SNAPSHOT_TIMEOUT = aiohttp.ClientTimeout(total=5)


def _rtsp_url(host: str, username: str, password: str, channel: int, stream: int = 0) -> str:
    """Build the RTSP stream URL for an XMEye channel."""
    h = sofia_hash(password)
    return (
        f"rtsp://{host}:554"
        f"/user={username}&password={h}&channel={channel + 1}&stream={stream}.sdp"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XMEyeCoordinator = entry.runtime_data
    async_add_entities(
        XMEyeCamera(coordinator, channel) for channel in range(coordinator.channel_count)
    )


class XMEyeCamera(Camera):
    """Camera entity providing RTSP stream, HTTP snapshot, and PTZ control."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: XMEyeCoordinator, channel: int) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._channel = channel
        self._remove_listener: Callable[[], None] | None = None

        entry = coordinator.entry
        self._host: str = entry.data[CONF_HOST]
        self._username: str = entry.data[CONF_USERNAME]
        self._password: str = entry.data[CONF_PASSWORD]

        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_camera"
        self._snapshot_path: str | None = None  # cached working snapshot path

    @property
    def name(self) -> str:
        return f"CH{self._channel + 1}"

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def available(self) -> bool:
        return self._coordinator.connected

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a JPEG snapshot from the device."""
        session = async_get_clientsession(self.hass)
        auth = aiohttp.BasicAuth(self._username, sofia_hash(self._password))

        # Try cached path first, then all known paths
        paths = (
            [self._snapshot_path] + _SNAPSHOT_PATHS
            if self._snapshot_path
            else _SNAPSHOT_PATHS
        )

        for path_tpl in paths:
            if path_tpl is None:
                continue
            path = path_tpl.format(channel=self._channel)
            url = f"http://{self._host}{path}"
            try:
                async with session.get(
                    url, auth=auth, timeout=_SNAPSHOT_TIMEOUT
                ) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        if "image" in content_type or resp.status == 200:
                            data = await resp.read()
                            if data:
                                self._snapshot_path = path_tpl  # cache working path
                                return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Snapshot attempt failed for %s: %s", url, err)

        _LOGGER.debug(
            "All snapshot URLs failed for ch%d on %s", self._channel + 1, self._host
        )
        return None

    async def stream_source(self) -> str | None:
        """Return the RTSP stream URL."""
        if not self._coordinator.connected:
            return None
        return _rtsp_url(self._host, self._username, self._password, self._channel)

    async def async_perform_ptz(
        self,
        pan: str | None = None,
        tilt: str | None = None,
        zoom: str | None = None,
        movement: str = "start",
        preset: int | None = None,
        speed: int | None = None,
    ) -> None:
        """Handle a PTZ command from HA."""
        if preset is not None:
            command = "GotoPreset"
        elif movement == "stop":
            command = "Stop"
        else:
            key = (
                tilt.upper() if tilt else None,
                pan.upper() if pan else None,
                zoom.upper() if zoom else None,
            )
            command = _PTZ_MAP.get(key)
            if command is None:
                _LOGGER.warning("Unrecognised PTZ movement: pan=%s tilt=%s zoom=%s", pan, tilt, zoom)
                return

        step = min(max(int(speed or 5), 1), 8)
        channel = self._channel

        await self._coordinator.async_run_command(
            lambda c: c.ptz_control(channel, command, step)
        )

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

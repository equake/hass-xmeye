"""Camera platform for XMEye/Sofia devices — RTSP stream, snapshot and PTZ."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .client import sofia_hash
from .coordinator import XMEyeCoordinator
from .entity import XMEyeEntity

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


class XMEyeCamera(XMEyeEntity, Camera):
    """Camera entity providing RTSP stream, HTTP snapshot, and PTZ control."""

    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: XMEyeCoordinator, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel

        entry = coordinator.entry
        self._host: str = entry.data[CONF_HOST]
        self._username: str = entry.data[CONF_USERNAME]
        self._password: str = entry.data[CONF_PASSWORD]

        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_camera"
        self._snapshot_path: str | None = None

    @property
    def name(self) -> str:
        return f"CH{self._channel + 1}"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a JPEG snapshot from the device."""
        session = async_get_clientsession(self.hass)
        auth = aiohttp.BasicAuth(self._username, sofia_hash(self._password))

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
                async with session.get(url, auth=auth, timeout=_SNAPSHOT_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if data:
                            self._snapshot_path = path_tpl
                            return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Snapshot attempt failed for %s: %s", url, err)

        _LOGGER.debug("All snapshot URLs failed for ch%d on %s", self._channel + 1, self._host)
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

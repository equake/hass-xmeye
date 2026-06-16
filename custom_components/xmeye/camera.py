"""Camera platform for XMEye/Sofia devices — RTSP stream, snapshot and PTZ."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re

import aiohttp
import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XMEyeConfigEntry
from .client import sofia_hash
from .const import SERVICE_PTZ, SIGNAL_NEW_CHANNEL
from .coordinator import XMEyeCoordinator
from .entity import XMEyeEntity

_LOGGER = logging.getLogger(__name__)

# Snapshot paths tried in order; first to return a valid JPEG frame is cached.
# Placeholders:
#   {channel}  = 0-based channel index (HA internal)
#   {channel1} = 1-based channel number (what most XMEye HTTP endpoints use)
#   {user} / {password} = plain-text credentials
#
# XMEye / Sofia HTTP snapshot API uses 1-based channel numbering.
# channel=0 falls through to channel 1 on tested firmware, so ch0 and ch1
# in HA would both return the same camera. RTSP also uses 1-based; we follow suit.
#
# The authenticated path is tried first and works for any device where credentials
# are set. The unauthenticated path covers devices that have no password configured.
_SNAPSHOT_PATHS = [
    # With credentials (plain-text in query string; works for NVR and password-protected IPC)
    "/webcapture.jpg?command=snap&channel={channel1}&user={user}&password={password}",
    # Without credentials (covers devices with no password set)
    "/webcapture.jpg?command=snap&channel={channel1}",
    # Legacy paths still found on some older firmwares (0-based, kept as fallback)
    "/snap.jpg?channel={channel}",
    "/web/cgi-bin/hi3510/snapPicture.cgi?chn={channel}",
    "/cgi-bin/snapshot.cgi?chn={channel}&q=0",
]

_JPEG_MAGIC = b"\xff\xd8\xff"
_SNAPSHOT_TIMEOUT = aiohttp.ClientTimeout(total=5)

# Minimum dimensions to accept a JPEG as a real video frame.
# Some firmwares return a tiny placeholder icon (e.g. 36x25) via /snap.jpg
# that passes the JPEG magic check but is not a real frame.
_MIN_SNAPSHOT_WIDTH = 320
_MIN_SNAPSHOT_HEIGHT = 240


def _jpeg_frame_size(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) parsed from JPEG SOF marker, or None on failure.

    Uses only stdlib — no Pillow dependency required.
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data) - 8:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # SOF0 / SOF1 / SOF2
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return w, h
        if i + 3 >= len(data):
            break
        seg_len = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seg_len
    return None


def _is_valid_frame(data: bytes) -> bool:
    """Return True if data is a JPEG with dimensions >= minimum video frame size."""
    if not data or data[:3] != _JPEG_MAGIC:
        return False
    size = _jpeg_frame_size(data)
    if size is None:
        return False
    return size[0] >= _MIN_SNAPSHOT_WIDTH and size[1] >= _MIN_SNAPSHOT_HEIGHT

_RTSP_PORT = 554
_RTSP_PROBE_TIMEOUT = 4.0


# ---------------------------------------------------------------------------
# RTSP probe helpers
# ---------------------------------------------------------------------------

def _rtsp_digest(username: str, password: str, method: str, uri: str, realm: str, nonce: str) -> str:
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (
        f'Digest username="{username}", realm="{realm}", '
        f'nonce="{nonce}", uri="{uri}", response="{response}"'
    )


async def _rtsp_has_video(url: str, host: str, username: str, password: str) -> bool:
    """Return True if the RTSP URL returns a valid SDP with a video track.

    Handles Digest auth challenge automatically.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, _RTSP_PORT),
            timeout=_RTSP_PROBE_TIMEOUT,
        )
        try:
            desc = f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n"
            writer.write(desc.encode())
            await writer.drain()
            resp = (
                await asyncio.wait_for(reader.read(4096), timeout=_RTSP_PROBE_TIMEOUT)
            ).decode(errors="replace")

            if "m=video" in resp:
                return True

            if "401" in resp.split("\r\n")[0]:
                m_realm = re.search(r'realm="([^"]+)"', resp)
                m_nonce = re.search(r'nonce="([^"]+)"', resp)
                if m_realm and m_nonce:
                    auth_hdr = _rtsp_digest(
                        username, password, "DESCRIBE", url,
                        m_realm.group(1), m_nonce.group(1),
                    )
                    desc2 = (
                        f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\n"
                        f"Accept: application/sdp\r\nAuthorization: {auth_hdr}\r\n\r\n"
                    )
                    writer.write(desc2.encode())
                    await writer.drain()
                    resp2 = (
                        await asyncio.wait_for(reader.read(8192), timeout=_RTSP_PROBE_TIMEOUT)
                    ).decode(errors="replace")
                    return "m=video" in resp2
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: XMEyeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XMEyeCoordinator = entry.runtime_data
    async_add_entities([
        XMEyeCamera(coordinator, ch) for ch in sorted(coordinator.connected_channels)
    ])

    def _on_new_channel(channel: int) -> None:
        async_add_entities([XMEyeCamera(coordinator, channel)])

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_CHANNEL.format(entry.entry_id), _on_new_channel
        )
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_PTZ,
        {
            vol.Required("command"): str,
            vol.Optional("movement", default="start"): vol.In(["start", "stop"]),
            vol.Optional("speed", default=5): vol.All(int, vol.Range(min=1, max=8)),
            vol.Optional("preset"): vol.All(int, vol.Range(min=0, max=255)),
        },
        "async_ptz_command",
    )


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------

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

        # Cached after first successful probe; None means "not yet discovered".
        self._snapshot_path: str | None = None
        self._stream_url: str | None = None

        # Locks prevent concurrent probes if HA calls us before the background task finishes.
        self._snapshot_lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        titles = self._coordinator.channel_titles
        if titles and self._channel < len(titles) and titles[self._channel]:
            return titles[self._channel]
        return f"CH{self._channel + 1}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_background_task(
            self._probe_urls(),
            name=f"xmeye_probe_{self._attr_unique_id}",
        )

    async def _probe_urls(self) -> None:
        """Probe snapshot and RTSP URLs concurrently at startup."""
        session = async_get_clientsession(self.hass)
        auth = aiohttp.BasicAuth(self._username, sofia_hash(self._password))
        await asyncio.gather(
            self._find_snapshot_path(session, auth),
            self._find_stream_url(),
        )

    # ------------------------------------------------------------------
    # Snapshot discovery
    # ------------------------------------------------------------------

    def _resolve_path(self, path_tpl: str) -> str:
        """Expand a snapshot path template with this camera's parameters."""
        return path_tpl.format(
            channel=self._channel,
            channel1=self._channel + 1,
            user=self._username,
            password=self._password,
        )

    async def _find_snapshot_path(
        self,
        session: aiohttp.ClientSession,
        auth: aiohttp.BasicAuth,
    ) -> None:
        """Try each snapshot path and cache the first that returns a real JPEG frame."""
        if self._snapshot_path is not None:
            return
        async with self._snapshot_lock:
            if self._snapshot_path is not None:
                return
            for path_tpl in _SNAPSHOT_PATHS:
                url = f"http://{self._host}" + self._resolve_path(path_tpl)
                try:
                    async with session.get(url, auth=auth, timeout=_SNAPSHOT_TIMEOUT) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if _is_valid_frame(data):
                                self._snapshot_path = path_tpl
                                size = _jpeg_frame_size(data)
                                _LOGGER.debug(
                                    "Snapshot URL cached for ch%d (%dx%d): %s",
                                    self._channel + 1, size[0], size[1], url,  # type: ignore[index]
                                )
                                return
                except (TimeoutError, aiohttp.ClientError):
                    pass
            _LOGGER.debug(
                "No working snapshot URL for ch%d on %s",
                self._channel + 1, self._host,
            )

    # ------------------------------------------------------------------
    # RTSP stream discovery
    # ------------------------------------------------------------------

    def _stream_url_candidates(self) -> list[str]:
        h = sofia_hash(self._password)
        ch = self._channel
        host = self._host
        user = self._username
        pwd = self._password
        return [
            # XMEye / Sofia standard format
            f"rtsp://{host}:{_RTSP_PORT}/user={user}&password={h}&channel={ch + 1}&stream=0.sdp",
            # Plain credentials in URL (some IPC firmwares, empty password)
            f"rtsp://{user}:{pwd}@{host}:{_RTSP_PORT}/",
            # Common IPC path variants
            f"rtsp://{host}:{_RTSP_PORT}/live/ch{ch}",
            f"rtsp://{host}:{_RTSP_PORT}/h264/ch{ch + 1}/main/av_stream",
        ]

    async def _find_stream_url(self) -> None:
        """Try each RTSP URL candidate and cache the first that serves a video track."""
        if self._stream_url is not None:
            return
        async with self._stream_lock:
            if self._stream_url is not None:
                return
            for url in self._stream_url_candidates():
                if await _rtsp_has_video(url, self._host, self._username, self._password):
                    self._stream_url = url
                    _LOGGER.debug(
                        "Stream URL cached for ch%d: %s",
                        self._channel + 1, url,
                    )
                    return
            _LOGGER.debug(
                "No working RTSP stream for ch%d on %s",
                self._channel + 1, self._host,
            )

    # ------------------------------------------------------------------
    # Camera interface
    # ------------------------------------------------------------------

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a JPEG snapshot, discovering the URL if not yet cached."""
        session = async_get_clientsession(self.hass)
        auth = aiohttp.BasicAuth(self._username, sofia_hash(self._password))

        if self._snapshot_path is None:
            await self._find_snapshot_path(session, auth)

        if self._snapshot_path is None:
            return None

        url = f"http://{self._host}" + self._resolve_path(self._snapshot_path)
        try:
            async with session.get(url, auth=auth, timeout=_SNAPSHOT_TIMEOUT) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if _is_valid_frame(data):
                        return data
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Snapshot failed for %s: %s", url, err)

        # Cached path no longer works — clear so the next call re-probes.
        self._snapshot_path = None
        return None

    async def stream_source(self) -> str | None:
        """Return the RTSP stream URL.

        Returns the probed (verified) URL once the background probe completes.
        Until then, falls back to the standard XMEye URL format so HA can
        attempt to connect immediately.
        """
        if not self._coordinator.connected:
            return None
        if self._stream_url is not None:
            return self._stream_url
        # Background probe still running — return the default URL as a best-effort.
        h = sofia_hash(self._password)
        return (
            f"rtsp://{self._host}:{_RTSP_PORT}"
            f"/user={self._username}&password={h}"
            f"&channel={self._channel + 1}&stream=0.sdp"
        )

    # ------------------------------------------------------------------
    # PTZ
    # ------------------------------------------------------------------

    async def async_ptz_command(
        self, command: str, movement: str = "start", speed: int = 5, preset: int | None = None
    ) -> None:
        """Handle a raw PTZ command from the xmeye.ptz service."""
        if preset is not None and command in ("GotoPreset", "SetPreset", "ClearPreset"):
            ptz_preset = preset
        else:
            # Preset=65535 starts the motor; Preset=-1 stops it (same command, same direction).
            ptz_preset = -1 if movement == "stop" else 65535
        step = min(max(speed, 1), 8)
        channel = self._channel
        await self._coordinator.async_run_command(
            lambda c: c.ptz_control(channel, command, step, ptz_preset)
        )

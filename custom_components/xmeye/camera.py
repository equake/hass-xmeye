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
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from . import XMEyeConfigEntry
from .client import sofia_hash
from .const import (
    CODEC_H264,
    CODEC_H265,
    CODEC_UNKNOWN,
    DOMAIN,
    GO2RTC_RTSP_PORT,
    SERVICE_PTZ,
    SIGNAL_NEW_CHANNEL,
)
from .coordinator import XMEyeCoordinator
from .entity import XMEyeEntity
from .go2rtc_client import Go2RTCClient

_LOGGER = logging.getLogger(__name__)

_REPAIR_H265_PREFIX = "h265_no_go2rtc"


def _go2rtc_stream_name(entry_id: str, channel: int) -> str:
    """Stable, unique name for a go2rtc stream entry (entity-scoped)."""
    return f"xmeye_{entry_id}_ch{channel + 1}"


def _h265_issue_id(entry_id: str, channel: int) -> str:
    """Stable repair-issue id for one (entry, channel) pair."""
    return f"{_REPAIR_H265_PREFIX}_{entry_id}_ch{channel + 1}"

# Snapshot paths tried in order; first to return a valid JPEG frame is cached.
# Placeholders:
#   {channel}  = 0-based channel index (HA internal)
#   {channel1} = 1-based channel number (what most XMEye HTTP endpoints use)
#   {user}     = username
#   {password} = sofia_hash of the password (same encoding RTSP and BasicAuth use;
#                the device accepts it in the query string and we avoid putting the
#                plain-text password in the URL)
#
# XMEye / Sofia HTTP snapshot API uses 1-based channel numbering.
# channel=0 falls through to channel 1 on tested firmware, so ch0 and ch1
# in HA would both return the same camera. RTSP also uses 1-based; we follow suit.
#
# The authenticated path is tried first and works for any device where credentials
# are set. The unauthenticated path covers devices that have no password configured.
_SNAPSHOT_PATHS = [
    # With credentials (sofia_hash in query string; works for NVR and password-protected IPC)
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

    Handles Digest auth challenge automatically. Used as a cheap fallback when
    the caller doesn't need codec info; prefer `_rtsp_probe` when it matters.
    """
    sdp, _ = await _rtsp_probe(url, host, username, password)
    return sdp is not None


def _sdp_video_codec(sdp: str) -> str:
    """Extract the video track codec from a RTSP SDP body.

    Returns CODEC_H264, CODEC_H265, or CODEC_UNKNOWN. Uses the `a=rtpmap`
    attribute of the first `m=video` line. XMEye / Sofia firmwares emit
    "H264" for H.264 and "H265" for H.265 (sometimes prefixed with the
    clock rate, e.g. "H264/90000"). Some HiSilicon firmwares spell it
    "HEVC" — treat as H.265.
    """
    in_video = False
    for line in sdp.splitlines():
        if line.startswith("m=video"):
            in_video = True
            continue
        if in_video:
            if line.startswith("m="):
                break  # next media section, video not yet resolved
            if line.startswith("a=rtpmap:"):
                # Format: "a=rtpmap:<payload-type> <encoding-name>/<clock>[/<params>]"
                _, _, payload = line.partition(":")
                parts = payload.strip().split("/", 1)
                if not parts or not parts[0]:
                    continue
                # Drop the leading payload-type number; what remains is "H264/90000".
                tokens = parts[0].split(None, 1)
                name = (tokens[1] if len(tokens) > 1 else tokens[0]).upper()
                if name.startswith("H264"):
                    return CODEC_H264
                if name.startswith("H265") or name.startswith("HEVC"):
                    return CODEC_H265
    return CODEC_UNKNOWN


async def _rtsp_probe(
    url: str, host: str, username: str, password: str
) -> tuple[str | None, str]:
    """Return (sdp_text_or_None, codec) for the given RTSP URL.

    Handles Digest auth challenge automatically. The SDP is returned
    only when the server actually served a media description (200 with
    `m=video`); on auth failure or transport error, returns (None, unknown).
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
                return resp, _sdp_video_codec(resp)

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
                    if "m=video" in resp2:
                        return resp2, _sdp_video_codec(resp2)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return None, CODEC_UNKNOWN


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
        # Codec of the chosen RTSP stream ("H264", "H265", "unknown").
        self._codec: str = CODEC_UNKNOWN
        # 0 = main, 1 = sub. Tracked for diagnostics only.
        self._stream_idx: int = 0
        # If we successfully registered a transcoded go2rtc stream, the
        # registered stream name; None otherwise. Used to clean up on
        # entity removal and to switch stream_source() to the go2rtc URL.
        self._go2rtc_stream_name: str | None = None

        # Locks prevent concurrent probes if HA calls us before the background task finishes.
        self._snapshot_lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        titles = self._coordinator.channel_titles
        if titles and self._channel < len(titles) and titles[self._channel]:
            return titles[self._channel]
        return f"CH{self._channel + 1}"

    @property
    def available(self) -> bool:
        # Privacy mode hides the camera entirely (no stream, no snapshot).
        return super().available and self._channel not in self._coordinator.private_channels

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_background_task(
            self._probe_urls(),
            name=f"xmeye_probe_{self._attr_unique_id}",
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        # Best-effort cleanup of any go2rtc stream we registered. Failures
        # are logged at debug only — the entity removal must not block.
        if self._go2rtc_stream_name is not None:
            client: Go2RTCClient | None = self._coordinator.go2rtc_client
            if client is not None:
                try:
                    await client.unregister_stream(self._go2rtc_stream_name)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "go2rtc unregister failed for %s", self._go2rtc_stream_name
                    )
            self._go2rtc_stream_name = None
        # Resolve any repair issue we created for this channel. Other
        # channels from the same device may still be H.265 — they will
        # re-create the issue on their own probe.
        if self._codec == CODEC_H265:
            try:
                async_delete_issue(
                    self.hass,
                    DOMAIN,
                    _h265_issue_id(self._coordinator.entry.entry_id, self._channel),
                )
            except Exception:  # noqa: BLE001
                pass

    async def _probe_urls(self) -> None:
        """Probe snapshot and RTSP URLs concurrently at startup, then
        arm go2rtc transcoding if the chosen stream is H.265-only."""
        session = async_get_clientsession(self.hass)
        auth = aiohttp.BasicAuth(self._username, sofia_hash(self._password))
        await asyncio.gather(
            self._find_snapshot_path(session, auth),
            self._find_stream_url(),
        )
        await self._maybe_register_go2rtc()

    async def _maybe_register_go2rtc(self) -> None:
        """If the chosen stream is H.265, register it with go2rtc for auto codec matching.

        Registers two sources:
        1. Original RTSP stream (H.265 for compatible clients like Companion App)
        2. FFmpeg transcoded stream (H.264 for browsers like Chrome/Linux)

        go2rtc will automatically select the appropriate source based on client capabilities.
        """
        if self._stream_url is None or self._codec != CODEC_H265:
            return
        client: Go2RTCClient | None = self._coordinator.go2rtc_client
        if client is None:
            return
        if not await client.is_available():
            _LOGGER.debug(
                "ch%d H.265 detected but go2rtc is not running — live view will "
                "only work in HEVC-capable browsers (Safari/macOS, Edge/Windows "
                "with HEVC extensions, mobile apps). Linux Chrome/Firefox will "
                "fall back to still-image snapshots.",
                self._channel + 1,
            )
            async_create_issue(
                self.hass,
                DOMAIN,
                _h265_issue_id(self._coordinator.entry.entry_id, self._channel),
                breaks_in_ha_version=None,
                is_fixable=False,
                is_persistent=False,
                severity=IssueSeverity.WARNING,
                translation_key="h265_no_go2rtc",
                translation_placeholders={
                    "channel": str(self._channel + 1),
                    "host": self._host,
                },
            )
            return
        name = _go2rtc_stream_name(self._coordinator.entry.entry_id, self._channel)

        _LOGGER.info(
            "ch%d H.265 detected — registering with go2rtc (H.265 native + H.264 transcoded)",
            self._channel + 1,
        )

        ok = await client.register_stream(name, self._stream_url, transcode_to_h264=True)
        if ok:
            self._go2rtc_stream_name = name
            _LOGGER.info(
                "ch%d stream registered with go2rtc as '%s' — auto codec matching enabled",
                self._channel + 1,
                name,
            )
        else:
            _LOGGER.warning(
                "ch%d H.265 stream — go2rtc registration failed; falling back to direct RTSP",
                self._channel + 1,
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
            password=sofia_hash(self._password),
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

    def _stream_url_candidates(self) -> list[tuple[str, int]]:
        """Yield (url, stream_index) candidates in preference order.

        For each URL shape we try the main stream (0, high-res) AND the
        sub stream (1, low-res). Preference is applied later in
        `_find_stream_url` based on parsed SDP codec: H.264 wins over
        H.265, and the sub stream is preferred for transcoding cost when
        only H.265 is available.
        """
        h = sofia_hash(self._password)
        ch = self._channel
        host = self._host
        user = self._username
        pwd = self._password
        return [
            # XMEye / Sofia standard format — main then sub
            (f"rtsp://{host}:{_RTSP_PORT}/user={user}&password={h}"
             f"&channel={ch + 1}&stream=0.sdp", 0),
            (f"rtsp://{host}:{_RTSP_PORT}/user={user}&password={h}"
             f"&channel={ch + 1}&stream=1.sdp", 1),
            # Plain credentials in URL (some IPC firmwares, empty password)
            (f"rtsp://{user}:{pwd}@{host}:{_RTSP_PORT}/", 0),
            # Common IPC path variants
            (f"rtsp://{host}:{_RTSP_PORT}/live/ch{ch}", 0),
            (f"rtsp://{host}:{_RTSP_PORT}/live/ch{ch}_1", 1),
            (f"rtsp://{host}:{_RTSP_PORT}/h264/ch{ch + 1}/main/av_stream", 0),
            (f"rtsp://{host}:{_RTSP_PORT}/h264/ch{ch + 1}/sub/av_stream", 1),
        ]

    async def _find_stream_url(self) -> None:
        """Probe every candidate and cache the best by codec.

        Best is defined as: first H.264 stream found (HLS-friendly, no
        transcoding needed in any browser); if none, the first H.265
        stream found (will need go2rtc transcoding for non-HEVC browsers);
        if none, the first stream with an unknown codec.
        """
        if self._stream_url is not None:
            return
        async with self._stream_lock:
            if self._stream_url is not None:
                return

            best: tuple[str, str, int] | None = None  # (url, codec, stream_idx)
            # Codec preference: H264=0, H265=1, unknown=2 (lower is better)
            rank = {CODEC_H264: 0, CODEC_H265: 1, CODEC_UNKNOWN: 2}

            for url, stream_idx in self._stream_url_candidates():
                sdp, codec = await _rtsp_probe(
                    url, self._host, self._username, self._password
                )
                if sdp is None:
                    continue
                candidate = (url, codec, stream_idx)
                if best is None or rank[codec] < rank[best[1]]:
                    best = candidate
                # Short-circuit on best possible codec
                if codec == CODEC_H264:
                    break

            if best is not None:
                self._stream_url, self._codec, self._stream_idx = best
                _LOGGER.debug(
                    "Stream URL cached for ch%d: codec=%s stream=%d url=%s",
                    self._channel + 1, self._codec, self._stream_idx, self._stream_url,
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
        if self._channel in self._coordinator.private_channels:
            return None
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
        """Return the URL HA should open for the live view.

        Resolution order:
        1. If we registered a transcoded go2rtc stream for this channel
           (H.265 input → H.264 output), return the go2rtc RTSP URL.
           HA-Core's bundled go2rtc will hand it to the browser as
           WebRTC H.264, which works on every browser.
        2. Otherwise return the probed RTSP URL (H.264 or other codec
           that the browser can decode natively via HLS).
        3. If the background probe is still running, return the standard
           XMEye URL as a best-effort so HA can attempt to connect
           immediately.
        """
        if not self._coordinator.connected:
            return None
        if self._channel in self._coordinator.private_channels:
            return None
        if self._go2rtc_stream_name is not None:
            return (
                f"rtsp://127.0.0.1:{GO2RTC_RTSP_PORT}/{self._go2rtc_stream_name}"
            )
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

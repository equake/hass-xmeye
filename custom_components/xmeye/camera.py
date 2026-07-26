"""Camera platform for XMEye/Sofia devices — RTSP stream, snapshot and PTZ."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import string
from collections.abc import Callable
from urllib.parse import quote

import aiohttp
import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
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
    CONF_TRANSCODE_BITRATE,
    CONF_TRANSCODE_H265,
    DEFAULT_TRANSCODE_BITRATE,
    DEFAULT_TRANSCODE_H265,
    DOMAIN,
    REPAIR_H265_NO_GO2RTC,
    REPAIR_H265_TRANSCODING,
    SERVICE_PTZ,
    SIGNAL_NEW_CHANNEL,
)
from .coordinator import XMEyeCoordinator
from .entity import XMEyeEntity
from .go2rtc_client import Go2RTCClient

_LOGGER = logging.getLogger(__name__)

# Mirror of homeassistant.components.go2rtc.util._SAFE_CHARS.
_GO2RTC_SAFE_CHARS = string.ascii_letters + string.digits + "._-"

_identifier_impl: Callable[[Camera], str] | None = None


def _fallback_identifier(camera: Camera) -> str:
    """Local copy of homeassistant.components.go2rtc.util.get_camera_identifier."""
    attr = camera.entity_id
    if camera.unique_id is not None:
        attr = f"{camera.platform.platform_name}_{camera.unique_id}"
    return quote(attr, safe=_GO2RTC_SAFE_CHARS)


def _go2rtc_identifier(camera: Camera) -> str:
    """Return the go2rtc stream name HA itself uses for this camera.

    We import HA's own helper when it is importable so we automatically
    track any change on its side; the local copy keeps us working on HA
    builds where the go2rtc component (or its requirements) is absent.
    """
    global _identifier_impl
    impl = _identifier_impl
    if impl is None:
        try:
            from homeassistant.components.go2rtc.util import get_camera_identifier

            impl = get_camera_identifier
        except Exception:  # noqa: BLE001 - optional component, any failure is fine
            impl = _fallback_identifier
        _identifier_impl = impl
    return impl(camera)


def _refresh_h265_issues(hass: HomeAssistant, coordinator: XMEyeCoordinator) -> None:
    """Rebuild this device's H.265 repair issues from coordinator state.

    Two mutually exclusive advisories, both dismissible:
    `h265_transcoding` when go2rtc is doing the H.265 → H.264 conversion
    (works, but costs CPU), `h265_no_go2rtc` when it cannot (live view
    degrades to still images in browsers without HEVC).
    """
    entry = coordinator.entry
    states = coordinator.h265_channels
    buckets = {
        REPAIR_H265_TRANSCODING: sorted(ch + 1 for ch, ok in states.items() if ok),
        REPAIR_H265_NO_GO2RTC: sorted(ch + 1 for ch, ok in states.items() if not ok),
    }
    for translation_key, channels in buckets.items():
        issue_id = f"{translation_key}_{entry.entry_id}"
        if not channels:
            async_delete_issue(hass, DOMAIN, issue_id)
            continue
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            breaks_in_ha_version=None,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "channels": ", ".join(str(ch) for ch in channels),
                "host": entry.data[CONF_HOST],
            },
        )

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
        # Privacy nulls stream_source(), which drops the WebRTC provider;
        # tracked so we can ask HA to re-evaluate when it is lifted.
        self._was_private = channel in coordinator.private_channels

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
        # Drop this channel from the device-wide H.265 advisory. Sibling
        # channels keep theirs.
        if self._coordinator.h265_channels.pop(self._channel, None) is not None:
            _refresh_h265_issues(self.hass, self._coordinator)

    @callback
    def _handle_update(self) -> None:
        # Leaving privacy gives the camera a stream source again. HA does not
        # re-evaluate WebRTC providers for that on its own, so nudge it.
        private = self._channel in self._coordinator.private_channels
        if private != self._was_private:
            self._was_private = private
            self.hass.async_create_task(self.async_refresh_providers())
        super()._handle_update()

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
        # HA decided whether to attach a WebRTC provider back in
        # async_internal_added_to_hass, before any of the above had run, and
        # it only re-evaluates on its own when a provider registers or the
        # STREAM feature bit flips. Ask it to look again now that there is a
        # real stream source — otherwise the camera stays HLS-only and
        # browsers that cannot decode H.265 fall back to the still-image
        # proxy at roughly one frame per second.
        await self.async_refresh_providers()

    async def _maybe_register_go2rtc(self) -> None:
        """Arm go2rtc transcoding if the chosen stream turned out to be H.265."""
        if self._stream_url is None or self._codec != CODEC_H265:
            return
        options = self._coordinator.entry.options
        if not options.get(CONF_TRANSCODE_H265, DEFAULT_TRANSCODE_H265):
            _LOGGER.info(
                "ch%d is H.265 but transcoding is disabled for this device — "
                "leaving the stream to Home Assistant's defaults",
                self._channel + 1,
            )
            # Registers nothing; called only so any advisory left over from
            # before it was switched off gets cleared.
            await self._ensure_go2rtc_stream(self._stream_url)
            return
        _LOGGER.info(
            "ch%d is H.265 — registering with go2rtc (H.265 passthrough + "
            "on-demand H.264 transcode capped at %d kbit/s)",
            self._channel + 1,
            int(options.get(CONF_TRANSCODE_BITRATE, DEFAULT_TRANSCODE_BITRATE)),
        )
        if not await self._ensure_go2rtc_stream(self._stream_url):
            _LOGGER.debug(
                "ch%d H.265 but go2rtc is not usable — live view will only work "
                "in HEVC-capable clients (Safari/macOS, Edge/Windows with the "
                "HEVC extensions, the mobile apps). Linux Chrome/Firefox will "
                "fall back to still-image snapshots.",
                self._channel + 1,
            )

    async def _ensure_go2rtc_stream(self, url: str) -> bool:
        """(Re)register this channel under the stream name HA itself uses.

        Two producers, in this order:

        1. the DVR's RTSP URL — H.265 handed through untouched to clients
           that can decode it (VLC, the mobile apps, Safari);
        2. ``ffmpeg:<identifier>#video=h264#audio=opus#bitrate=…`` — go2rtc's
           ``AddConsumer`` walks producers in order and only dials this one
           when the previous producer's codecs do not match the consumer, so
           the transcode costs nothing until a browser without HEVC asks.

        Registering under HA's own identifier, with source 1 byte-identical
        to what :meth:`stream_source` returns, is what stops HA's go2rtc
        provider from replacing the stream with its audio-only variant —
        see ``homeassistant/components/go2rtc/__init__.py``:
        ``_update_stream_source`` skips the rewrite when the stream exists
        and one of its producers matches ``await camera.stream_source()``.

        Returns False without touching go2rtc when the user has turned
        transcoding off for this device.
        """
        options = self._coordinator.entry.options
        if not options.get(CONF_TRANSCODE_H265, DEFAULT_TRANSCODE_H265):
            # Deliberate choice by the user: register nothing and let HA
            # handle the H.265 stream its own way. Dropping the channel also
            # clears both advisories — nobody should be nagged about a
            # trade-off they made on purpose.
            if self._coordinator.h265_channels.pop(self._channel, None) is not None:
                _refresh_h265_issues(self.hass, self._coordinator)
            return False

        client: Go2RTCClient | None = self._coordinator.go2rtc_client
        identifier = _go2rtc_identifier(self)
        bitrate = int(options.get(CONF_TRANSCODE_BITRATE, DEFAULT_TRANSCODE_BITRATE))
        sources = [
            url,
            f"ffmpeg:{identifier}#video=h264#audio=opus#bitrate={bitrate}k",
        ]
        ok = False
        if client is not None:
            try:
                ok = await client.ensure_stream(identifier, sources)
            except Exception:  # never break the live view
                _LOGGER.debug(
                    "ch%d go2rtc registration raised", self._channel + 1, exc_info=True
                )
        if ok:
            self._go2rtc_stream_name = identifier
        # Keep the device-wide advisory in step: go2rtc coming or going flips
        # which of the two repair issues applies.
        if self._coordinator.h265_channels.get(self._channel) != ok:
            self._coordinator.h265_channels[self._channel] = ok
            _refresh_h265_issues(self.hass, self._coordinator)
        return ok

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
                self._coordinator.stream_info[self._channel] = {
                    "codec": self._codec,
                    "stream_idx": self._stream_idx,
                    "url": re.sub(r"password=[^&]*", "password=***", self._stream_url),
                    "go2rtc_identifier": _go2rtc_identifier(self),
                }
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

    def _default_stream_url(self) -> str:
        """Standard XMEye main-stream URL, used until the probe finishes."""
        h = sofia_hash(self._password)
        return (
            f"rtsp://{self._host}:{_RTSP_PORT}"
            f"/user={self._username}&password={h}"
            f"&channel={self._channel + 1}&stream=0.sdp"
        )

    async def stream_source(self) -> str | None:
        """Return the DVR's RTSP URL for the live view.

        Never gated on ``coordinator.connected``: RTSP does not go through
        the DVRIP alarm socket, and HA asks for the stream source while
        deciding whether to attach a WebRTC provider — which happens before
        the coordinator's background connection loop has logged in. Returning
        None there would leave the camera HLS-only for the rest of its life.

        Always the camera's own URL — never a go2rtc loopback URL. Chaining
        go2rtc through its own RTSP server collapses the codec negotiation
        (an RTSP consumer takes the first matching video track and stops
        looking, so it would always land on H.265), and it would break the
        HLS/recording path, which cannot read a go2rtc-only source.

        For H.265 channels this is also the hook where we repair the go2rtc
        registration: HA's provider calls ``stream_source()`` immediately
        before deciding whether to overwrite the stream, so re-asserting our
        two producers here means the state is already correct by the time it
        looks — and self-heals if anything ever replaced it.
        """
        if self._channel in self._coordinator.private_channels:
            return None
        url = self._stream_url or self._default_stream_url()
        if self._codec == CODEC_H265:
            await self._ensure_go2rtc_stream(url)
        return url

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

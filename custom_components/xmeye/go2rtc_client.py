"""Minimal HTTP client for the go2rtc HTTP API.

Used to auto-register H.265-only streams so go2rtc transcodes them to H.264
for WebRTC playback in browsers that cannot decode H.265 in HLS/MSE
(Linux Chrome/Chromium/Firefox, etc.).

go2rtc is bundled with Home Assistant Core since 2024.11 (port 11984) and
also runs standalone (port 1984). We probe both and remember whichever
responds first per coordinator; if neither does, the integration falls
back gracefully to the raw RTSP URL (current behavior — H.265 stream
will only work in H.265-capable browsers).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .const import GO2RTC_PORTS, GO2RTC_STREAMS_PATH

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=4)


class Go2RTCClient:
    """Pooled HTTP client to the local go2rtc instance.

    One instance per coordinator. Probe-once on first call to figure out
    which port is live (11984 = HA-Core bundled, 1984 = legacy/standalone).
    After that all calls go to that port.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._base_url: str | None = None
        self._rtsp_port: int | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> str | None:
        """Return the working base URL or None if go2rtc is unreachable.

        Holds the lock so concurrent first-callers don't race the probe.
        Also discovers the RTSP port from the go2rtc API.
        """
        if self._base_url is not None:
            return self._base_url
        async with self._lock:
            if self._base_url is not None:
                return self._base_url
            if self._session is None:
                self._session = aiohttp.ClientSession()
            for port in GO2RTC_PORTS:
                url = f"http://127.0.0.1:{port}{GO2RTC_STREAMS_PATH}"
                try:
                    async with self._session.get(url, timeout=_TIMEOUT) as resp:
                        if resp.status < 500:
                            self._base_url = f"http://127.0.0.1:{port}"
                            _LOGGER.debug("go2rtc reachable at %s", self._base_url)
                            await self._discover_rtsp_port()
                            return self._base_url
                except (TimeoutError, aiohttp.ClientError, OSError):
                    continue
            _LOGGER.debug("go2rtc not reachable on any of %s", GO2RTC_PORTS)
            return None

    async def _discover_rtsp_port(self) -> None:
        """Discover the RTSP port from the go2rtc API."""
        if self._base_url is None or self._session is None:
            return
        try:
            async with self._session.get(
                f"{self._base_url}/api",
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rtsp_listen = data.get("rtsp", {}).get("listen", "")
                    if ":" in rtsp_listen:
                        port_str = rtsp_listen.rsplit(":", 1)[1]
                        self._rtsp_port = int(port_str)
                        _LOGGER.debug("go2rtc RTSP port discovered: %d", self._rtsp_port)
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
            _LOGGER.debug("Failed to discover go2rtc RTSP port: %s", err)

    @property
    def rtsp_port(self) -> int:
        """Return the RTSP port (discovered from go2rtc API, or 8554 as fallback)."""
        return self._rtsp_port or 8554

    async def is_available(self) -> bool:
        """Return True if go2rtc answered the probe; cache the result."""
        return (await self._ensure()) is not None

    async def register_stream(
        self,
        name: str,
        source_url: str,
    ) -> bool:
        """Register a stream with go2rtc using multiple sources for auto codec matching.

        Registers two sources:
        1. Original RTSP stream (H.265 for compatible clients like VLC, Companion App)
        2. FFmpeg transcoded stream (H.264 for browsers like Chrome/Linux via WebRTC)

        The second source uses auto-reference (ffmpeg:{stream_name}) which allows
        go2rtc to transcode the stream on-demand. go2rtc will automatically select
        the appropriate source based on client capabilities.
        """
        base = await self._ensure()
        if base is None:
            return False

        # Build list of source URLs
        # Multiple `src` query params = multiple sources for the same stream
        sources = [
            source_url,
            # FFmpeg source uses auto-reference to the stream itself
            # go2rtc will transcode H.265 → H.264 on-demand
            f"ffmpeg:{name}#video=h264#audio=opus",
        ]

        # Use repeated `src` query parameter for multiple sources
        params = [("src", s) for s in sources] + [("name", name)]

        try:
            async with self._session.put(
                f"{base}{GO2RTC_STREAMS_PATH}",
                params=params,
                timeout=_TIMEOUT,
            ) as resp:
                ok = resp.status < 300
                if ok:
                    _LOGGER.info(
                        "go2rtc stream registered: name=%s sources=%d (H.265 + H.264 transcoded)",
                        name,
                        len(sources),
                    )
                else:
                    _LOGGER.warning(
                        "go2rtc PUT /api/streams returned %s for %s",
                        resp.status,
                        name,
                    )
                return ok
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("go2rtc register_stream failed for %s: %s", name, err)
            return False

    async def unregister_stream(self, name: str) -> bool:
        """Remove a stream from go2rtc. Best-effort: failures are logged only."""
        base = await self._ensure()
        if base is None or self._session is None:
            return False
        try:
            async with self._session.delete(
                f"{base}{GO2RTC_STREAMS_PATH}",
                params={"name": name},
                timeout=_TIMEOUT,
            ) as resp:
                ok = resp.status < 300
                if ok:
                    _LOGGER.debug("go2rtc stream unregistered: name=%s", name)
                return ok
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("go2rtc unregister_stream failed for %s: %s", name, err)
            return False

    async def close(self) -> None:
        """Close the shared session. Idempotent."""
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._base_url = None

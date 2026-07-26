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
from typing import Any

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
        self._lock = asyncio.Lock()

    async def _ensure(self) -> str | None:
        """Return the working base URL or None if go2rtc is unreachable.

        Holds the lock so concurrent first-callers don't race the probe.
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
                            return self._base_url
                except (TimeoutError, aiohttp.ClientError, OSError):
                    continue
            _LOGGER.debug("go2rtc not reachable on any of %s", GO2RTC_PORTS)
            return None

    async def is_available(self) -> bool:
        """Return True if go2rtc answered the probe; cache the result."""
        return (await self._ensure()) is not None

    async def register_stream(
        self,
        name: str,
        source_url: str,
        *,
        transcode_to_h264: bool = True,
    ) -> bool:
        """Register a stream with go2rtc. Idempotent (PUT upserts by name).

        If `transcode_to_h264` is True, attaches an ffmpeg consumer that
        re-encodes the video to H.264 (the original audio, when present,
        is copied). This is the lever that makes H.265 streams watchable
        in Linux browsers — the registered RTSP endpoint serves H.264.
        """
        base = await self._ensure()
        if base is None:
            return False
        payload: dict[str, Any] = {
            "name": name,
            "urls": [source_url],
        }
        if transcode_to_h264:
            payload["ffmpeg"] = "-c:v libx264 -preset ultrafast -tune zerolatency -an"
        try:
            async with self._session.put(  # type: ignore[union-attr]
                f"{base}{GO2RTC_STREAMS_PATH}",
                json=payload,
                timeout=_TIMEOUT,
            ) as resp:
                ok = resp.status < 300
                if ok:
                    _LOGGER.debug(
                        "go2rtc stream registered: name=%s source=%s transcode=%s",
                        name, source_url, transcode_to_h264,
                    )
                else:
                    _LOGGER.warning(
                        "go2rtc PUT /api/streams returned %s for %s", resp.status, name
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

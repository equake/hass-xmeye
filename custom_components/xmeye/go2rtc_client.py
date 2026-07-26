"""Minimal HTTP client for the go2rtc HTTP API.

Used to register H.265-only channels as multi-source go2rtc streams so that
go2rtc transcodes them to H.264 on demand for browsers that cannot decode
HEVC (Linux Chrome/Chromium/Firefox), while still handing raw H.265 to
clients that can (VLC, the mobile apps, Safari).

Reaching go2rtc is the tricky part, because Home Assistant supports several
very different deployments:

* **HA-managed binary, default.** ``homeassistant/components/go2rtc/server.py``
  writes ``api.listen: ""`` — there is *no* TCP port at all. The API is only
  on a unix socket in a ``mkdtemp(prefix="go2rtc-")`` directory, behind
  BasicAuth with credentials generated at every start.
* **HA-managed with ``go2rtc: debug_ui: true``.** Same, plus TCP 11984 —
  still behind ``local_auth: true``.
* **External instance** (``go2rtc: url: http://host:1984``), with or without
  ``username``/``password``.

Rather than guessing, we reuse the ``ClientSession`` and base URL the go2rtc
integration already built (``hass.data["go2rtc"]``): it carries the right
connector (``UnixConnector`` or TCP) and the right credentials for whichever
of the above the user runs. That is private HA API, so it is probed
defensively and we fall back to scanning the well-known localhost ports for
a go2rtc that HA does not know about.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import unquote

import aiohttp
from homeassistant.core import HomeAssistant

from .const import GO2RTC_PORTS, GO2RTC_STREAMS_PATH

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=4)

# hass.data key the go2rtc integration stores its Go2RtcConfig(url, session)
# under. HassKey is a str subclass, so a plain string lookup is equivalent.
_GO2RTC_DATA_KEY = "go2rtc"

# How long to stop looking for go2rtc after a failed resolution. Callers hit
# ensure_stream() on every stream_source() call, and stream_source() sits on
# the live-view path — re-probing every time would stall it.
_UNAVAILABLE_TTL = 60.0


class Go2RTCClient:
    """Pooled HTTP client to whichever go2rtc instance this HA talks to.

    One instance per coordinator. The endpoint is resolved once, on first
    use, and cached; a connection error drops the cache so the next call
    re-resolves (the unix socket path changes when go2rtc respawns).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._session: aiohttp.ClientSession | None = None
        # False when the session belongs to the go2rtc integration and must
        # not be closed by us.
        self._owns_session = False
        self._base_url: str | None = None
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Endpoint resolution
    # ------------------------------------------------------------------

    async def _ensure(self) -> str | None:
        """Return a usable base URL, or None if go2rtc is unreachable."""
        if self._base_url is not None:
            return self._base_url
        if time.monotonic() < self._retry_after:
            return None
        async with self._lock:
            if self._base_url is not None:
                return self._base_url
            if self._adopt_ha_client():
                return self._base_url
            if (base := await self._probe_ports()) is None:
                self._retry_after = time.monotonic() + _UNAVAILABLE_TTL
            return base

    def _adopt_ha_client(self) -> bool:
        """Reuse the session/URL configured by the `go2rtc` integration.

        Covers every deployment shape at once (unix socket, TCP, external,
        with or without auth). Private API, hence the defensive getattr.
        """
        try:
            config = self._hass.data.get(_GO2RTC_DATA_KEY)
            url = getattr(config, "url", None)
            session = getattr(config, "session", None)
            if not url or session is None or session.closed:
                return False
        except Exception:  # never let this break the camera
            _LOGGER.debug("Could not read hass.data['go2rtc']", exc_info=True)
            return False
        self._base_url = str(url).rstrip("/")
        self._session = session
        self._owns_session = False
        _LOGGER.debug("Using the go2rtc integration's own client (%s)", self._base_url)
        return True

    async def _probe_ports(self) -> str | None:
        """Fallback: scan localhost for a go2rtc that HA does not manage."""
        if self._session is None or not self._owns_session:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        for port in GO2RTC_PORTS:
            base = f"http://127.0.0.1:{port}"
            try:
                async with self._session.get(
                    f"{base}{GO2RTC_STREAMS_PATH}", timeout=_TIMEOUT
                ) as resp:
                    if resp.status == 200:
                        self._base_url = base
                        _LOGGER.debug("go2rtc reachable at %s", base)
                        return base
                    # 401/403 means something is listening but we cannot
                    # drive it — claiming availability would only produce a
                    # failing PUT later on.
                    _LOGGER.debug(
                        "go2rtc on port %d answered %s — no usable credentials",
                        port, resp.status,
                    )
            except (TimeoutError, aiohttp.ClientError, OSError):
                continue
        _LOGGER.debug("go2rtc not reachable on any of %s", GO2RTC_PORTS)
        return None

    # ------------------------------------------------------------------
    # Stream management
    # ------------------------------------------------------------------

    async def ensure_stream(self, name: str, sources: list[str]) -> bool:
        """Make sure `name` is registered with `sources`, idempotently.

        The common case is a single local GET that finds the stream already
        correct; the PUT only happens when it is missing or was rewritten by
        somebody else. Returns True when the stream is in the desired state.
        """
        base = await self._ensure()
        if base is None or self._session is None:
            return False
        if await self._stream_has_sources(base, name, sources):
            return True
        return await self._put_stream(base, name, sources)

    async def _stream_has_sources(
        self, base: str, name: str, sources: list[str]
    ) -> bool:
        """Return True if the go2rtc stream `name` carries every source.

        A producer's JSON `url` is the configured source string while it is
        idle, but the *connection's* URL once it is dialled. For an
        `ffmpeg:` source go2rtc rewrites that to a loopback RTSP URL which
        still carries the original source percent-encoded in `?source=`, so
        an unquoted substring match recognises both states without ever
        re-PUTting (and tearing down) a stream that is working.
        """
        try:
            async with self._session.get(  # type: ignore[union-attr]
                f"{base}{GO2RTC_STREAMS_PATH}", timeout=_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
            _LOGGER.debug("go2rtc stream list failed: %s", err)
            self._base_url = None
            return False

        stream = data.get(name) if isinstance(data, dict) else None
        if not isinstance(stream, dict):
            return False
        urls = [
            unquote(producer["url"])
            for producer in stream.get("producers") or []
            if isinstance(producer, dict) and isinstance(producer.get("url"), str)
        ]
        return all(any(source in url for url in urls) for source in sources)

    async def _put_stream(self, base: str, name: str, sources: list[str]) -> bool:
        """Replace the go2rtc stream `name` with exactly `sources`."""
        # Repeated `src` query params = multiple sources for one stream.
        params = [("src", source) for source in sources] + [("name", name)]
        try:
            async with self._session.put(  # type: ignore[union-attr]
                f"{base}{GO2RTC_STREAMS_PATH}", params=params, timeout=_TIMEOUT
            ) as resp:
                if resp.status < 300:
                    _LOGGER.info(
                        "go2rtc stream registered: name=%s sources=%s", name, sources
                    )
                    return True
                _LOGGER.warning(
                    "go2rtc PUT %s returned %s for %s",
                    GO2RTC_STREAMS_PATH, resp.status, name,
                )
                return False
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("go2rtc register failed for %s: %s", name, err)
            self._base_url = None
            return False

    async def unregister_stream(self, name: str) -> bool:
        """Remove a stream from go2rtc. Best-effort: failures are logged only.

        DELETE is keyed off ``src``, not ``name`` — ``PUT`` is the verb that
        takes ``name``. And a request that reaches ``/api/streams`` without a
        ``src`` short-circuits into "list every stream" and answers 200
        (``internal/streams/api.go``), so getting this wrong is a no-op that
        reports success. Hence the empty-name guard.
        """
        base = await self._ensure()
        if base is None or self._session is None or not name:
            return False
        try:
            async with self._session.delete(
                f"{base}{GO2RTC_STREAMS_PATH}", params={"src": name}, timeout=_TIMEOUT
            ) as resp:
                ok = resp.status < 300
                if ok:
                    _LOGGER.debug("go2rtc stream unregistered: name=%s", name)
                return ok
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("go2rtc unregister failed for %s: %s", name, err)
            return False

    async def close(self) -> None:
        """Release the session if we own it. Idempotent."""
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None
        self._owns_session = False
        self._base_url = None

"""Coordinator for XMEye/Sofia alarm integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .client import AlarmEvent, XMEyeAuthError, XMEyeClient
from .const import (
    CONF_CHANNEL_COUNT,
    CONF_DEVICE_TYPE,
    CONF_NAME_ENCODE,
    CONF_NAME_ENCODE_ALT,
    CONF_NAME_GENERAL,
    CONF_NAME_MOTION,
    CONF_NAME_STORAGE,
    CONF_NAME_STORAGE_ALT,
    CONF_NAME_STORAGE_LEGACY,
    DOMAIN,
    MIN_SNAPSHOT_BYTES,
    RECONNECT_DELAY,
    SIGNAL_NEW_CHANNEL,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

_STORAGE_REFRESH_INTERVAL = timedelta(seconds=60)
_CHANNEL_RECHECK_INTERVAL = timedelta(minutes=5)

# Some IPC/DVR firmwares use different names for the same alarm event type.
# Normalize them to the canonical names used in binary_sensor.py so that
# sensors fire regardless of which firmware variant sent the event.
_EVENT_ALIASES: dict[str, str] = {
    "VideoMotion": "MotionDetect",     # IPC firmware variant
    "Motion": "MotionDetect",          # rare short form
    "VideoLoss": "VideoLost",          # some firmwares spell it without 't'
    "BlindDetect": "HideAlarm",        # alternate name for video blind/tamper
    "LocalAlarm": "AlarmLocal",        # alternate order seen on some DVRs
}


class XMEyeCoordinator:
    """Manages the persistent connection to one XMEye/Sofia device.

    All platforms register listeners here. Alarm events arrive via the
    persistent connection; one-shot commands (PTZ, config get/set, reboot)
    use short-lived secondary connections via async_run_command().
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        # Persisted across reconnects
        self.channel_count: int = entry.data.get(CONF_CHANNEL_COUNT, 1)
        self.device_type: str = entry.data.get(CONF_DEVICE_TYPE, "XMEye Device")
        self.connected: bool = False

        # (channel_index, event_type_string) → True/False
        self.states: dict[tuple[int, str], bool] = {}

        # Cached device & storage info (populated after first login)
        self.device_info_cache: dict[str, str | None] = {}
        self.storage_cache: list[dict[str, Any]] = []

        # Encode config name that worked for this device (cached after first use)
        self._encode_cfg_name: str | None = None

        # Channels confirmed to have a camera connected.
        # Populated by HTTP probe at startup; updated by events and periodic recheck.
        self.connected_channels: set[int] = set()

        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._command_lock = asyncio.Lock()
        self._unsub_refresh: Callable[[], None] | None = None
        self._unsub_channel_check: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        # Probe HTTP snapshot to determine which channels have cameras.
        # Runs before async_forward_entry_setups() so platforms can use
        # connected_channels to create only the relevant entities.
        await self._detect_connected_channels()

        self._task = self.hass.async_create_background_task(
            self._connection_loop(),
            name=f"xmeye_{self.entry.entry_id}",
            eager_start=True,
        )
        self._unsub_refresh = async_track_time_interval(
            self.hass,
            self._async_refresh_storage,
            _STORAGE_REFRESH_INTERVAL,
        )
        self._unsub_channel_check = async_track_time_interval(
            self.hass,
            self._async_recheck_channels,
            _CHANNEL_RECHECK_INTERVAL,
        )

    async def async_shutdown(self) -> None:
        if self._unsub_channel_check:
            self._unsub_channel_check()
            self._unsub_channel_check = None
        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # Shared DeviceInfo (used by all entity platforms)
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.device_type or "XMEye Device",
            manufacturer="Xiongmai / XMEye",
            model=self.device_info_cache.get("model") or self.device_type,
            sw_version=self.device_info_cache.get("firmware"),
            serial_number=self.device_info_cache.get("serial"),
        )

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a state-change listener; returns a removal callable."""
        self._listeners.append(callback)

        def _remove() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

        return _remove

    def _notify_listeners(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in listener callback")

    # ------------------------------------------------------------------
    # Command execution (short-lived secondary connection)
    # ------------------------------------------------------------------

    async def async_run_command(
        self, fn: Callable[[XMEyeClient], Awaitable[_T]]
    ) -> _T:
        """Open a short-lived authenticated connection, run fn(client), close.

        Serialised by _command_lock so concurrent callers queue up safely.
        """
        host = self.entry.data[CONF_HOST]
        port = self.entry.data[CONF_PORT]
        username = self.entry.data[CONF_USERNAME]
        password = self.entry.data[CONF_PASSWORD]

        async with self._command_lock:
            client = XMEyeClient(host, port, username, password)
            try:
                await client.connect()
                await client.login()
                return await fn(client)
            finally:
                await client.close()

    # ------------------------------------------------------------------
    # Config helpers (used by switch.py)
    # ------------------------------------------------------------------

    async def async_get_encode_cfg(self) -> tuple[str, list]:
        """Return (config_name, channel_list) for the encode config."""
        async def _get(client: XMEyeClient) -> tuple[str, list]:
            for name in (CONF_NAME_ENCODE, CONF_NAME_ENCODE_ALT):
                data = await client.config_get(name)
                if data:
                    self._encode_cfg_name = name
                    return name, data if isinstance(data, list) else [data]
            return CONF_NAME_ENCODE, []

        return await self.async_run_command(_get)

    async def async_set_motion_enabled(self, channel: int, enabled: bool) -> None:
        async def _set(client: XMEyeClient) -> None:
            data = await client.config_get(CONF_NAME_MOTION)
            if not data:
                return
            entries = data if isinstance(data, list) else [data]
            if channel < len(entries):
                entries[channel]["Enable"] = enabled
            await client.config_set(CONF_NAME_MOTION, entries)

        await self.async_run_command(_set)

    async def async_set_recording_enabled(self, channel: int, enabled: bool) -> None:
        async def _set(client: XMEyeClient) -> None:
            cfg_name = self._encode_cfg_name or CONF_NAME_ENCODE
            data = await client.config_get(cfg_name)
            if not data:
                # try alternate
                cfg_name_alt = CONF_NAME_ENCODE_ALT if cfg_name == CONF_NAME_ENCODE else CONF_NAME_ENCODE
                data = await client.config_get(cfg_name_alt)
                if data:
                    cfg_name = cfg_name_alt
                    self._encode_cfg_name = cfg_name
            if not data:
                _LOGGER.warning("Could not read encode config to set recording state")
                return
            entries = data if isinstance(data, list) else [data]
            if channel < len(entries):
                enc = entries[channel]
                if "MainFormat" in enc:
                    mf = enc["MainFormat"]
                    if "VideoEnable" in mf:
                        # IPC firmware: top-level VideoEnable flag
                        mf["VideoEnable"] = enabled
                    else:
                        # DVR firmware: nested Video.Enable
                        mf.setdefault("Video", {})["Enable"] = enabled
                elif "Video" in enc:
                    enc["Video"]["Enable"] = enabled
                else:
                    enc["Enable"] = enabled
            await client.config_set(cfg_name, entries)

        await self.async_run_command(_set)

    # ------------------------------------------------------------------
    # Channel detection (which slots have a camera connected)
    # ------------------------------------------------------------------

    async def _probe_channel_http(
        self,
        session: aiohttp.ClientSession,
        host: str,
        user: str,
        pwd: str,
        channel: int,
    ) -> bool:
        """Return True if a real JPEG frame is served for this channel."""
        ch1 = channel + 1
        url = (
            f"http://{host}/webcapture.jpg"
            f"?command=snap&channel={ch1}&user={user}&password={pwd}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.read()
                return data[:3] == b"\xff\xd8\xff" and len(data) > MIN_SNAPSHOT_BYTES
        except Exception:  # noqa: BLE001
            return False

    async def _detect_connected_channels(self) -> None:
        """Probe all channels concurrently and populate connected_channels.

        Falls back to all channels if HTTP is not available on the device.
        """
        host = self.entry.data[CONF_HOST]
        user = self.entry.data[CONF_USERNAME]
        pwd = self.entry.data[CONF_PASSWORD]

        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *[
                    self._probe_channel_http(session, host, user, pwd, ch)
                    for ch in range(self.channel_count)
                ],
                return_exceptions=True,
            )

        found = {ch for ch, ok in enumerate(results) if ok is True}
        if found:
            self.connected_channels = found
            _LOGGER.debug(
                "Connected channels on %s: %s",
                host,
                sorted(ch + 1 for ch in found),
            )
        else:
            # HTTP snapshot not available — assume all channels have cameras.
            self.connected_channels = set(range(self.channel_count))
            _LOGGER.debug(
                "Channel probe failed for %s — assuming all %d channels connected",
                host,
                self.channel_count,
            )

    async def _async_recheck_channels(self, _now: object = None) -> None:
        """Periodically probe channels not yet confirmed, dispatch signal for new ones."""
        unchecked = set(range(self.channel_count)) - self.connected_channels
        if not unchecked:
            return

        host = self.entry.data[CONF_HOST]
        user = self.entry.data[CONF_USERNAME]
        pwd = self.entry.data[CONF_PASSWORD]

        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *[self._probe_channel_http(session, host, user, pwd, ch) for ch in unchecked],
                return_exceptions=True,
            )

        for ch, ok in zip(sorted(unchecked), results, strict=False):
            if ok is True:
                self.connected_channels.add(ch)
                _LOGGER.debug("New camera detected on channel %d", ch + 1)
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_NEW_CHANNEL.format(self.entry.entry_id),
                    ch,
                )

    # ------------------------------------------------------------------
    # Storage refresh (periodic + initial)
    # ------------------------------------------------------------------

    async def _async_refresh_storage(self, _now: object = None) -> None:
        """Fetch storage info via a short-lived connection and notify listeners."""
        try:
            await self.async_run_command(self._fetch_storage)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Storage refresh failed (device may be offline)")
        else:
            self._notify_listeners()

    async def _fetch_storage(self, client: XMEyeClient) -> None:
        for name in (CONF_NAME_STORAGE, CONF_NAME_STORAGE_ALT, CONF_NAME_STORAGE_LEGACY):
            raw = await client.config_get(name)
            if not raw:
                continue
            entries = raw if isinstance(raw, list) else [raw]
            parsed = [self._parse_storage_entry(e) for e in entries]
            # Only accept entries that actually carry capacity data
            if any(e.get("total_gb") for e in parsed):
                self.storage_cache = parsed
                return
        _LOGGER.debug(
            "HDD capacity info not available on %s (tried %s, %s, %s)",
            self.entry.data.get("host", "device"),
            CONF_NAME_STORAGE,
            CONF_NAME_STORAGE_ALT,
            CONF_NAME_STORAGE_LEGACY,
        )

    @staticmethod
    def _parse_storage_entry(entry: dict) -> dict[str, Any]:
        total_raw = entry.get("TotalSpace", 0)
        used_raw = entry.get("UsedSpace", 0)
        free_raw = entry.get("FreeSpace", 0)

        # Devices report in different units (MB or sectors x 512).
        # Values > 100_000 are likely in MB; otherwise assume GB directly.
        def to_gb(val: int | float) -> float:
            if val > 100_000:
                return round(val / 1024, 1)  # MB → GB
            return float(val)

        return {
            "name": entry.get("Name", entry.get("StorageName", "HDD")),
            "status": entry.get("Status", entry.get("State", "Unknown")),
            "total_gb": to_gb(total_raw),
            "used_gb": to_gb(used_raw),
            "free_gb": to_gb(free_raw),
        }

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except XMEyeAuthError as err:
                _LOGGER.error(
                    "Authentication failed for %s - check credentials: %s",
                    self.entry.data[CONF_HOST],
                    err,
                )
                raise ConfigEntryAuthFailed(str(err)) from err
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "XMEye connection lost for %s: %s - retrying in %ds",
                    self.entry.data[CONF_HOST],
                    err,
                    RECONNECT_DELAY,
                )

            if self.connected:
                self.connected = False
                self._notify_listeners()

            await asyncio.sleep(RECONNECT_DELAY)

    async def _run_once(self) -> None:
        host = self.entry.data[CONF_HOST]
        port = self.entry.data[CONF_PORT]
        username = self.entry.data[CONF_USERNAME]
        password = self.entry.data[CONF_PASSWORD]

        client = XMEyeClient(host, port, username, password)
        try:
            await client.connect()
            login_info = await client.login()

            if login_info.channel_count != self.channel_count:
                self.channel_count = login_info.channel_count
            if login_info.device_type and login_info.device_type != self.device_type:
                self.device_type = login_info.device_type

            # Fetch device info and storage before entering the alarm loop
            await self._fetch_device_info_direct(client)
            await self._fetch_storage(client)

            self.connected = True
            self._notify_listeners()

            await client.subscribe_alarms()

            keepalive_task = asyncio.create_task(
                self._keepalive_loop(client, login_info.keepalive_interval),
                name=f"xmeye_keepalive_{self.entry.entry_id}",
            )
            try:
                async for event in client.read_events():
                    self._handle_event(event)
            finally:
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
        finally:
            await client.close()

    async def _fetch_device_info_direct(self, client: XMEyeClient) -> None:
        """Fetch General config using the already-logged-in alarm client."""
        try:
            raw = await client.config_get(CONF_NAME_GENERAL)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(raw, dict):
            return
        # The "General" config is a nested object.  Top-level keys are sub-sections
        # (AdaptEncode, AutoLogin, …).  Device name lives inside the inner "General"
        # sub-section; firmware/serial may be at the top level on some firmwares.
        inner = raw.get("General", {}) if isinstance(raw, dict) else {}
        self.device_info_cache = {
            "firmware": (raw.get("Firmware") or raw.get("Version")
                         or inner.get("Firmware") or inner.get("Version")),
            "serial": (raw.get("Serial") or raw.get("SerialNo")
                       or inner.get("Serial") or inner.get("SerialNo")),
            "model": raw.get("MachineName") or inner.get("MachineName") or self.device_type,
        }

    def _handle_event(self, event: AlarmEvent) -> None:
        event_type = _EVENT_ALIASES.get(event.event_type, event.event_type)

        # Any event from a channel is proof that a camera is connected there.
        if event.channel not in self.connected_channels:
            self.connected_channels.add(event.channel)
            _LOGGER.debug("Camera on channel %d confirmed via alarm event", event.channel + 1)
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_CHANNEL.format(self.entry.entry_id),
                event.channel,
            )

        key = (event.channel, event_type)
        if self.states.get(key) != event.active:
            self.states[key] = event.active
            self._notify_listeners()

    async def _keepalive_loop(self, client: XMEyeClient, interval: int) -> None:
        effective_interval = max(10, interval - 5)
        try:
            while True:
                await asyncio.sleep(effective_interval)
                await client.keepalive()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Keepalive error - main loop will handle reconnect")

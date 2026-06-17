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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .client import AlarmEvent, XMEyeAuthError, XMEyeClient
from .const import (
    CONF_CHANNEL_COUNT,
    CONF_DEVICE_TYPE,
    CONF_MOTION_CLEAR_DELAY,
    CONF_NAME_DETECT,
    CONF_NAME_GENERAL,
    CONF_NAME_RECORD,
    CONF_NAME_STORAGE,
    CONF_NAME_SYSFUNCTION,
    CONF_STORAGE_REFRESH_INTERVAL,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEFAULT_STORAGE_REFRESH_INTERVAL,
    DETECT_KINDS,
    DOMAIN,
    MAX_STORAGE_REFRESH_INTERVAL,
    MIN_SNAPSHOT_BYTES,
    MIN_STORAGE_REFRESH_INTERVAL,
    RECONNECT_DELAY,
    RECORD_MODE_OFF,
    RECORD_MODE_SCHEDULE,
    RET_OK,
    SIGNAL_NEW_CHANNEL,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

_CHANNEL_RECHECK_INTERVAL = timedelta(minutes=5)

# Event types whose Stop is debounced. Discrete/physical events are excluded.
_DEBOUNCED_EVENTS = {"MotionDetect", "CrossLineDetection", "PEAAlarm"}

# Best-effort decode of StorageInfo partition fields (reverse-engineered; only the
# normal value 0 has been observed). Unknown codes are logged at debug and fall back
# to a generic label / the "error" state, so the enum never trusts an unverified code.
_HDD_DRIVER_TYPES = {0: "read_write", 1: "read_only", 2: "redundant", 3: "snapshot"}
_KNOWN_HDD_STATUS = {0}

# Some IPC/DVR firmwares use different names for the same alarm event type.
# Normalize them to the canonical names used in binary_sensor.py so that
# sensors fire regardless of which firmware variant sent the event.
_EVENT_ALIASES: dict[str, str] = {
    "VideoMotion": "MotionDetect",              # IPC firmware variant
    "Motion": "MotionDetect",                   # rare short form
    "appEventHumanDetectAlarm": "MotionDetect", # AI human-detect on XMEye IPC/NVR
    "FaceDetect": "MotionDetect",               # face-detect on HVR/NVR firmware
    "VideoLoss": "VideoLost",                   # some firmwares spell it without 't'
    "BlindDetect": "HideAlarm",                 # alternate name for video blind/tamper
    "LocalAlarm": "AlarmLocal",                 # alternate order seen on some DVRs
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

        # Per-channel control state, fetched on connect (and after writes).
        # detect_cache: {kind -> list-of-dicts per channel}; record_cache: list per channel.
        self.detect_cache: dict[str, list] = {}
        self.record_cache: list[dict[str, Any]] = []
        self.system_function: dict[str, Any] = {}
        # Channels the user has put in privacy mode (HA-side): camera hidden + not recording.
        self.private_channels: set[int] = set()
        # RecordMode to restore when leaving privacy / turning recording back on.
        self._record_restore: dict[int, str] = {}

        # Channels confirmed to have a camera connected.
        # Populated by HTTP probe at startup; updated by events and periodic recheck.
        self.connected_channels: set[int] = set()

        # Channel titles (names) from ChannelTitle config (cmd 1048).
        # Index N = name of channel N. Populated after first login.
        self.channel_titles: list[str] = []

        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._command_lock = asyncio.Lock()
        self._unsub_refresh: Callable[[], None] | None = None
        self._unsub_channel_check: Callable[[], None] | None = None
        # Pending debounce timers: (channel, event_type) → unsub callable
        self._clear_unsubs: dict[tuple[int, str], Callable[[], None]] = {}

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
        interval = int(self.entry.options.get(
            CONF_STORAGE_REFRESH_INTERVAL, DEFAULT_STORAGE_REFRESH_INTERVAL
        ))
        interval = max(MIN_STORAGE_REFRESH_INTERVAL, min(MAX_STORAGE_REFRESH_INTERVAL, interval))
        self._unsub_refresh = async_track_time_interval(
            self.hass,
            self._async_refresh_storage,
            timedelta(seconds=interval),
        )
        self._unsub_channel_check = async_track_time_interval(
            self.hass,
            self._async_recheck_channels,
            _CHANNEL_RECHECK_INTERVAL,
        )

    async def async_shutdown(self) -> None:
        for unsub in self._clear_unsubs.values():
            unsub()
        self._clear_unsubs.clear()
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

    async def async_get_channel_titles(self) -> list[str]:
        """Fetch channel titles from the device via cmd 1048.

        Returns a list where index N is the name of channel N.
        Used to map camera names (e.g. 'Externa') to channel indices.
        """
        async def _get(client: XMEyeClient) -> list[str]:
            return await client.channel_title()

        return await self.async_run_command(_get)

    # ------------------------------------------------------------------
    # Per-channel controls (detection / recording / privacy)
    # ------------------------------------------------------------------

    async def async_refresh_controls(self) -> None:
        """Populate the control caches via a short-lived connection (used at setup)."""
        await self.async_run_command(self._fetch_controls)

    async def _fetch_controls(self, client: XMEyeClient) -> None:
        """Fetch detection/recording state + capabilities (on the persistent socket).

        Reads the per-kind Detect sub-lists and the Record list (small), plus the
        SystemFunction capability flags (cmd 1360). Cached for the switch entities.
        """
        self.system_function = await client.ability_get(CONF_NAME_SYSFUNCTION)
        detect: dict[str, list] = {}
        for _kind, (subkey, _cap) in DETECT_KINDS.items():
            ret, val = await client.config_get_raw(f"{CONF_NAME_DETECT}.{subkey}")
            if ret in RET_OK and isinstance(val, list):
                detect[subkey] = val
        self.detect_cache = detect
        rec = await client.config_get(CONF_NAME_RECORD)
        self.record_cache = rec if isinstance(rec, list) else []

    def detect_supported(self, kind: str, channel: int) -> bool:
        """True if this detection kind is usable for the channel on this device."""
        subkey, cap = DETECT_KINDS[kind]
        alarm = self.system_function.get("AlarmFunction", {}) if self.system_function else {}
        # If capabilities were read, require the flag; otherwise fall back to shape.
        if alarm and not alarm.get(cap, False):
            return False
        block = self.detect_cache.get(subkey)
        return (
            isinstance(block, list)
            and channel < len(block)
            and isinstance(block[channel], dict)
            and "Enable" in block[channel]
        )

    def detect_enabled(self, kind: str, channel: int) -> bool | None:
        subkey, _cap = DETECT_KINDS[kind]
        block = self.detect_cache.get(subkey) or []
        if channel < len(block) and isinstance(block[channel], dict):
            return bool(block[channel].get("Enable"))
        return None

    async def async_set_detect_enabled(self, kind: str, channel: int, enabled: bool) -> None:
        """Toggle a detection kind for one channel via the per-channel Detect section.

        GET the whole per-channel sub-section, change only Enable, SET it back — the
        full Detect block is ~155 KB and times out, and rebuilding would wipe the
        sensitivity/region masks.
        """
        subkey, _cap = DETECT_KINDS[kind]
        name = f"{CONF_NAME_DETECT}.{subkey}.[{channel}]"

        async def _set(client: XMEyeClient) -> None:
            ret, cfg = await client.config_get_raw(name)
            if ret not in RET_OK or not isinstance(cfg, dict):
                _LOGGER.warning("Could not read %s (Ret=%s)", name, ret)
                return
            cfg["Enable"] = enabled
            await client.config_set(name, cfg)
            self.detect_cache.setdefault(subkey, [])
            if channel < len(self.detect_cache[subkey]):
                self.detect_cache[subkey][channel]["Enable"] = enabled

        await self.async_run_command(_set)
        self._notify_listeners()

    def record_mode(self, channel: int) -> str | None:
        if channel < len(self.record_cache):
            return self.record_cache[channel].get("RecordMode")
        return None

    def recording_on(self, channel: int) -> bool | None:
        mode = self.record_mode(channel)
        return None if mode is None else mode != RECORD_MODE_OFF

    async def async_set_record_mode(self, channel: int, mode: str) -> None:
        """Set RecordMode for one channel (GET the Record list, change one, SET)."""
        async def _set(client: XMEyeClient) -> None:
            rec = await client.config_get(CONF_NAME_RECORD)
            if not isinstance(rec, list) or channel >= len(rec):
                _LOGGER.warning("Could not read Record config for ch%d", channel + 1)
                return
            rec[channel]["RecordMode"] = mode
            await client.config_set(CONF_NAME_RECORD, rec)
            self.record_cache = rec

        await self.async_run_command(_set)
        self._notify_listeners()

    async def async_set_recording_on(self, channel: int, on: bool) -> None:
        """Recording switch: ON restores the remembered mode, OFF closes recording."""
        if on:
            mode = self._record_restore.pop(channel, RECORD_MODE_SCHEDULE)
        else:
            current = self.record_mode(channel)
            if current and current != RECORD_MODE_OFF:
                self._record_restore[channel] = current
            mode = RECORD_MODE_OFF
        await self.async_set_record_mode(channel, mode)

    async def async_set_privacy(self, channel: int, private: bool) -> None:
        """HA-side privacy: stop recording + hide the camera entity (no device encode lever)."""
        if private:
            self.private_channels.add(channel)
            await self.async_set_recording_on(channel, False)
        else:
            self.private_channels.discard(channel)
            await self.async_set_recording_on(channel, True)
        self._notify_listeners()

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
        # Storage is runtime info via SystemInfo (cmd 1020); ConfigGet (1042) returns Ret=607.
        raw = await client.system_info(CONF_NAME_STORAGE)
        disks = [d for d in (raw if isinstance(raw, list) else [raw]) if isinstance(d, dict)]
        if disks:
            # Keep zero-capacity entries too: they surface as the "no_disk" status.
            self.storage_cache = [self._parse_storage_entry(d) for d in disks]
            return
        _LOGGER.debug(
            "HDD info not available on %s (queried %s via SystemInfo)",
            self.entry.data.get("host", "device"),
            CONF_NAME_STORAGE,
        )

    @staticmethod
    def _parse_storage_entry(disk: dict) -> dict[str, Any]:
        """Map one physical disk (a SystemInfo StorageInfo entry) to sensor fields.

        Spaces are hex strings in MB (e.g. ``"0x001D1C11"``); older firmwares may send
        plain ints. Each disk holds a ``Partition`` list with TotalSpace/RemainSpace.
        Capacity and ``IsCurrent`` are reliable; the ``Status``/``DirverType`` ints are
        best-effort (see _HDD_DRIVER_TYPES / _KNOWN_HDD_STATUS).
        """
        def to_mb(val: object) -> int:
            if isinstance(val, str):
                val = val.strip()
                try:
                    return int(val, 16) if val.lower().startswith("0x") else int(val or 0)
                except ValueError:
                    return 0
            if isinstance(val, (int, float)):
                return int(val)
            return 0

        def clean_time(val: object) -> str | None:
            text = str(val or "").strip()
            return None if not text or text.startswith("0000-00-00") else text

        partitions = disk.get("Partition") or []
        total_mb = sum(to_mb(p.get("TotalSpace", 0)) for p in partitions)
        remain_mb = sum(to_mb(p.get("RemainSpace", 0)) for p in partitions)
        used_mb = max(total_mb - remain_mb, 0)

        sized = [p for p in partitions if to_mb(p.get("TotalSpace", 0)) > 0]
        for part in sized:
            code = int(part.get("Status", 0) or 0)
            if code not in _KNOWN_HDD_STATUS:
                _LOGGER.debug("Unknown HDD partition Status=%s (treated as error)", code)
        has_fault = any(int(p.get("Status", 0) or 0) != 0 for p in sized)

        if total_mb == 0:
            status = "no_disk"
        elif has_fault:
            status = "error"
        elif remain_mb == 0:
            status = "full"  # full and recycling — normal DVR overwrite mode
        else:
            status = "ok"

        # Details come from the partition currently being recorded to (else first sized).
        current = next(
            (p for p in partitions if p.get("IsCurrent")),
            sized[0] if sized else {},
        )
        driver_type = int(current.get("DirverType", 0) or 0)

        return {
            "name": disk.get("ModelNumber") or disk.get("SerialNumber") or "HDD",
            "status": status,
            "total_gb": round(total_mb / 1024, 1),
            "used_gb": round(used_mb / 1024, 1),
            "free_gb": round(remain_mb / 1024, 1),
            "used_pct": round(used_mb / total_mb * 100, 1) if total_mb else None,
            "model": disk.get("ModelNumber") or None,
            "serial": disk.get("SerialNumber") or None,
            "partition_count": len(partitions),
            "read_write": _HDD_DRIVER_TYPES.get(driver_type, f"type_{driver_type}"),
            "driver_type": driver_type,
            "status_code": int(current.get("Status", 0) or 0),
            "is_recording": bool(current.get("IsCurrent")),
            "oldest_record": clean_time(current.get("OldStartTime")),
            "newest_record": clean_time(current.get("NewEndTime")),
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
            await self._fetch_channel_titles_direct(client)
            try:
                await self._fetch_controls(client)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not fetch per-channel controls", exc_info=True)

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
        """Populate device_info_cache using the already-logged-in alarm client.

        Firmware/serial/hardware live in SystemInfo (cmd 1020), NOT in the General
        config (cmd 1042) — General only carries the friendly device name (MachineName).
        """
        info: dict = {}
        try:
            raw_info = await client.system_info("SystemInfo")
            if isinstance(raw_info, dict):
                info = raw_info
        except Exception:  # noqa: BLE001
            pass

        # The "General" config is nested: top-level keys are sub-sections (AdaptEncode,
        # AutoLogin, …); the device name lives inside the inner "General" sub-section.
        general: dict = {}
        try:
            raw = await client.config_get(CONF_NAME_GENERAL)
            if isinstance(raw, dict):
                general = {**raw, **(raw.get("General") or {})}
        except Exception:  # noqa: BLE001
            pass

        if not info and not general:
            return  # keep any previously cached values on transient failure

        # Model: hardware code + configured name, e.g. "NBD80X16S-KL (LocalHost)".
        hardware = info.get("HardWare")
        machine = general.get("MachineName")
        if hardware and machine:
            model = f"{hardware} ({machine})"
        else:
            model = hardware or machine or self.device_type

        self.device_info_cache = {
            "firmware": (info.get("SoftWareVersion") or info.get("Version")
                         or general.get("Firmware") or general.get("Version")),
            "serial": (info.get("SerialNo") or info.get("Serial")
                       or general.get("SerialNo") or general.get("Serial")),
            "model": model,
        }

    async def _fetch_channel_titles_direct(self, client: XMEyeClient) -> None:
        """Fetch channel titles (cmd 1048) using the already-logged-in alarm client."""
        try:
            titles = await client.channel_title()
        except Exception:  # noqa: BLE001
            return
        if titles:
            self.channel_titles = titles
            _LOGGER.debug("Channel titles: %s", titles)

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

        if event.active:
            # Cancel any pending debounce clear so the sensor stays ON.
            if key in self._clear_unsubs:
                self._clear_unsubs.pop(key)()
            if not self.states.get(key):
                self.states[key] = True
                self._notify_listeners()
        elif event_type in _DEBOUNCED_EVENTS:
            # Delay the clear; ignore if sensor is already OFF or timer already running.
            delay = int(self.entry.options.get(CONF_MOTION_CLEAR_DELAY, DEFAULT_MOTION_CLEAR_DELAY))
            if delay > 0 and self.states.get(key) and key not in self._clear_unsubs:
                def _make_clear(k: tuple[int, str], d: int) -> None:
                    @callback
                    def _do_clear(_now: object) -> None:
                        self._clear_unsubs.pop(k, None)
                        if self.states.get(k):
                            self.states[k] = False
                            self._notify_listeners()
                    self._clear_unsubs[k] = async_call_later(self.hass, d, _do_clear)
                _make_clear(key, delay)
            elif delay == 0 and self.states.get(key):
                self.states[key] = False
                self._notify_listeners()
        else:
            # Non-debounced events (VideoLost, HideAlarm, etc.) clear immediately.
            if self.states.get(key):
                self.states[key] = False
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

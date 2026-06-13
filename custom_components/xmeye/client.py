"""Async XMEye/Sofia DVRIP protocol client."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .const import (
    MSG_ALARM_NOTIFY,
    MSG_ALARM_SUBSCRIBE,
    MSG_CONFIG_GET,
    MSG_CONFIG_SET,
    MSG_KEEPALIVE,
    MSG_LOGIN,
    MSG_PTZ_CONTROL,
    RET_OK,
)

_LOGGER = logging.getLogger(__name__)

# DVRIP packet header: magic(B) req/resp(B) reserved(xx) session(I) seq(I) total(B) cur(B) cmd(H) length(I)
# Total: 1+1+2+4+4+1+1+2+4 = 20 bytes
_HEADER_FMT = "<BB2xII2xHI"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 20

_MAGIC = 0xFF
_REQ_BYTE = 0x00  # second byte for requests
_SOFIA_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_CONNECT_TIMEOUT = 10.0
_RECV_TIMEOUT = 60.0


def sofia_hash(password: str) -> str:
    """XMEye/Sofia MD5-based password hash."""
    digest = hashlib.md5(password.encode("utf-8")).digest()
    return "".join(
        _SOFIA_CHARS[(digest[i * 2] + digest[i * 2 + 1]) % 62] for i in range(8)
    )


@dataclass
class LoginInfo:
    session_id: int
    channel_count: int
    device_type: str
    keepalive_interval: int


@dataclass
class AlarmEvent:
    channel: int
    event_type: str
    active: bool  # True = Start, False = Stop


class XMEyeAuthError(Exception):
    """Raised when login credentials are rejected."""


class XMEyeClient:
    """Async DVRIP/XMEye client for alarm event subscription."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session_id: int = 0
        self._seq: int = 0

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=_CONNECT_TIMEOUT,
        )
        _LOGGER.debug("Connected to %s:%d", self._host, self._port)

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._writer = None
                self._reader = None

    async def login(self) -> LoginInfo:
        body = {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": sofia_hash(self._password),
            "UserName": self._username,
        }
        await self._send(MSG_LOGIN, body)
        _, resp = await self._recv()

        ret = resp.get("Ret", 0)
        if ret not in RET_OK:
            raise XMEyeAuthError(f"Login rejected, code={ret}")

        self._session_id = int(resp["SessionID"], 16)

        # Some devices include a trailing space in the key name (firmware quirk)
        device_type = (resp.get("DeviceType") or resp.get("DeviceType ") or "XMEye").strip()

        _LOGGER.debug(
            "Logged in: session=0x%08X channels=%d device=%s",
            self._session_id,
            resp.get("ChannelNum", 1),
            device_type,
        )
        return LoginInfo(
            session_id=self._session_id,
            channel_count=int(resp.get("ChannelNum", 1)),
            device_type=device_type,
            keepalive_interval=int(resp.get("AliveInterval", 20)),
        )

    async def subscribe_alarms(self) -> None:
        """Send alarm subscription request (cmd 1500)."""
        body = {"Name": "", "SessionID": f"0x{self._session_id:08X}"}
        await self._send(MSG_ALARM_SUBSCRIBE, body)
        try:
            _, resp = await asyncio.wait_for(self._recv(), timeout=5.0)
            _LOGGER.debug("Alarm subscribe response: %s", resp)
        except TimeoutError:
            # Some devices do not acknowledge the subscription
            _LOGGER.debug("No alarm subscribe response (device may still push events)")

    async def keepalive(self) -> None:
        """Send a keepalive packet to maintain the session."""
        body = {"Name": "KeepAlive", "SessionID": f"0x{self._session_id:08X}"}
        await self._send(MSG_KEEPALIVE, body)

    async def config_get(self, name: str) -> dict:
        """Fetch a named config block via cmd 1042. Returns the inner dict or {} on failure."""
        body = {"Name": name, "SessionID": f"0x{self._session_id:08X}"}
        await self._send(MSG_CONFIG_GET, body)
        _, resp = await asyncio.wait_for(self._recv(), timeout=10.0)
        ret = resp.get("Ret", 0)
        if ret not in RET_OK:
            _LOGGER.debug("config_get(%s) failed, Ret=%s", name, ret)
            return {}
        return resp.get(name) or {}

    async def config_set(self, name: str, value: object) -> bool:
        """Write a named config block via cmd 1040. Returns True on success."""
        body = {"Name": name, "SessionID": f"0x{self._session_id:08X}", name: value}
        await self._send(MSG_CONFIG_SET, body)
        _, resp = await asyncio.wait_for(self._recv(), timeout=10.0)
        ret = resp.get("Ret", 0)
        if ret not in RET_OK:
            _LOGGER.debug("config_set(%s) failed, Ret=%s", name, ret)
        return ret in RET_OK

    async def ptz_control(self, channel: int, command: str, step: int = 5) -> None:
        """Send a PTZ command via cmd 1400."""
        body = {
            "Name": "OPPTZControl",
            "SessionID": f"0x{self._session_id:08X}",
            "OPPTZControl": {
                "Command": command,
                "Parameter": {
                    "Channel": channel,
                    "Step": step,
                    "Preset": -1,
                    "Tour": 0,
                    "Pattern": 0,
                },
            },
        }
        await self._send(MSG_PTZ_CONTROL, body)
        try:
            await asyncio.wait_for(self._recv(), timeout=2.0)
        except TimeoutError:
            pass  # many firmwares do not ACK PTZ commands

    async def reboot(self) -> None:
        """Reboot the device via OPMachine."""
        body = {
            "Name": "OPMachine",
            "SessionID": f"0x{self._session_id:08X}",
            "OPMachine": {"Action": "Reboot"},
        }
        await self._send(MSG_CONFIG_SET, body)
        try:
            await asyncio.wait_for(self._recv(), timeout=3.0)
        except TimeoutError:
            pass  # device may disconnect immediately on reboot

    async def read_events(self) -> AsyncIterator[AlarmEvent]:
        """Async generator that yields alarm events from the device.

        Runs until the connection is closed or an I/O error occurs.
        """
        while True:
            try:
                msg_id, body = await asyncio.wait_for(self._recv(), timeout=_RECV_TIMEOUT)
            except TimeoutError as exc:
                raise ConnectionError("Receive timeout — connection likely lost") from exc
            except asyncio.IncompleteReadError as exc:
                raise ConnectionError("Connection closed by device") from exc

            if msg_id != MSG_ALARM_NOTIFY:
                continue

            # Body: {"Name": "AlarmInfo", "AlarmInfo": {...}, "SessionID": "0x..."}
            name = body.get("Name", "AlarmInfo")
            data = body.get(name)
            if not isinstance(data, dict) or not data:
                continue

            channel = int(data.get("Channel", 0))
            event_type = str(data.get("Event", ""))
            status = str(data.get("Status", "Stop"))

            if event_type:
                _LOGGER.debug(
                    "Alarm event: channel=%d type=%s status=%s",
                    channel,
                    event_type,
                    status,
                )
                yield AlarmEvent(
                    channel=channel,
                    event_type=event_type,
                    active=(status == "Start"),
                )

    # ------------------------------------------------------------------
    # Low-level send / receive
    # ------------------------------------------------------------------

    async def _send(self, msg_id: int, body: dict) -> None:
        # Payload: JSON body + newline terminator (device convention)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\x0a\x00"
        header = struct.pack(
            _HEADER_FMT,
            _MAGIC,
            _REQ_BYTE,
            self._session_id,
            self._seq,
            msg_id,
            len(payload),
        )
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._writer.write(header + payload)
        await self._writer.drain()

    async def _recv(self) -> tuple[int, dict]:
        assert self._reader is not None
        header_raw = await self._reader.readexactly(_HEADER_SIZE)

        magic, _resp_byte, _sid, _seq, msg_id, length = struct.unpack(_HEADER_FMT, header_raw)
        if magic != _MAGIC:
            raise ValueError(f"Unexpected magic byte: {magic:#x}")

        body: dict = {}
        if length > 0:
            raw = await self._reader.readexactly(length)
            try:
                body = json.loads(raw.rstrip(b"\x00\x0a"))
            except json.JSONDecodeError as exc:
                _LOGGER.debug("Failed to parse JSON body (cmd=%d): %s | raw=%r", msg_id, exc, raw[:120])

        return msg_id, body

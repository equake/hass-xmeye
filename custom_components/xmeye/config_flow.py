"""Config flow for XMEye/Sofia integration — manual entry + LAN discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import XMEyeAuthError, XMEyeClient
from .const import (
    CONF_CHANNEL_COUNT,
    CONFIG_ENTRY_VERSION,
    CONF_DEVICE_TYPE,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    UDP_DISCOVERY_PORT,
)

_LOGGER = logging.getLogger(__name__)

_MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
            int, vol.Range(min=1, max=65535)
        ),
        vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Optional(CONF_PASSWORD, default=""): str,
    }
)

# DVRIP "find" broadcast packet — standard device-discovery request used by
# XMEye apps (cmd 0x5A0 / 1440, empty body, session=0)
_DISCOVERY_PACKET = struct.pack("<BB2xII2xHI", 0xFF, 0x00, 0, 0, 1530, 0)
_DISCOVERY_TIMEOUT = 3.0


async def async_discover_devices(timeout: float = _DISCOVERY_TIMEOUT) -> list[dict[str, str]]:
    """Broadcast a DVRIP discovery query and collect device responses.

    Returns a list of {"host": "<ip>", "name": "<device name>"} dicts.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, dict[str, str]] = {}

    def _broadcast() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)
            try:
                sock.sendto(_DISCOVERY_PACKET, ("255.255.255.255", UDP_DISCOVERY_PORT))
            except OSError as err:
                _LOGGER.debug("Discovery broadcast failed: %s", err)
                return

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(4096)
                    ip = addr[0]
                    if ip not in found:
                        # Try to parse device name from the JSON payload (offset 20)
                        name = ip
                        if len(data) > 20:
                            try:
                                body = json.loads(data[20:].rstrip(b"\x00\x0a"))
                                name = (
                                    body.get("DeviceID")
                                    or body.get("Name")
                                    or body.get("MachineName")
                                    or ip
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        found[ip] = {"host": ip, "name": str(name)}
                except socket.timeout:
                    break
                except OSError:
                    break

    await loop.run_in_executor(None, _broadcast)
    return list(found.values())


async def _test_credentials(
    host: str, port: int, username: str, password: str
) -> dict[str, Any]:
    """Open a short-lived connection, log in, return extra data for the config entry."""
    client = XMEyeClient(host, port, username, password)
    try:
        await client.connect()
        info = await client.login()
    finally:
        await client.close()

    return {
        CONF_CHANNEL_COUNT: info.channel_count,
        CONF_DEVICE_TYPE: info.device_type,
    }


class XMEyeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for XMEye/Sofia devices."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        super().__init__()
        self._discovered: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Step 1 — manual entry (default entry point)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # "Scan" button — transition to discovery step
            if user_input.get("action") == "scan":
                return await self.async_step_discovery()

            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            try:
                extra = await asyncio.wait_for(
                    _test_credentials(host, port, username, password), timeout=15.0
                )
            except XMEyeAuthError:
                errors["base"] = "invalid_auth"
            except (asyncio.TimeoutError, TimeoutError):
                errors["base"] = "cannot_connect"
            except OSError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during XMEye setup")
                errors["base"] = "unknown"
            else:
                device_name = extra.get(CONF_DEVICE_TYPE) or host
                data = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    **extra,
                }
                return self.async_create_entry(title=device_name, data=data)

        # Add a scan trigger button via a hidden field (HA renders it as a link)
        schema = vol.Schema(
            {
                **_MANUAL_SCHEMA.schema,
                vol.Optional("action"): vol.In(["scan"]),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=_MANUAL_SCHEMA,
            errors=errors,
            description_placeholders={"scan_hint": ""},
        )

    # ------------------------------------------------------------------
    # Step 2 — LAN discovery
    # ------------------------------------------------------------------

    async def async_step_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            selected_host = user_input["host"]
            # Pre-fill the manual form with the discovered host
            return await self.async_step_user(
                {
                    CONF_HOST: selected_host,
                    CONF_PORT: DEFAULT_PORT,
                    CONF_USERNAME: DEFAULT_USERNAME,
                    CONF_PASSWORD: "",
                }
            )

        self._discovered = await async_discover_devices()
        if not self._discovered:
            return self.async_show_form(
                step_id="discovery",
                data_schema=vol.Schema({}),
                description_placeholders={"found": "0"},
                errors={"base": "discovery_no_devices"},
            )

        options = {d["host"]: f"{d['name']} ({d['host']})" for d in self._discovered}
        return self.async_show_form(
            step_id="discovery",
            data_schema=vol.Schema(
                {
                    vol.Required("host"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": k, "label": v} for k, v in options.items()
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"found": str(len(self._discovered))},
        )

    # ------------------------------------------------------------------
    # Reconfigure — update connection settings post-setup
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        reconfigure_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD] or reconfigure_entry.data[CONF_PASSWORD]

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_mismatch()

            try:
                extra = await asyncio.wait_for(
                    _test_credentials(host, port, username, password), timeout=15.0
                )
            except XMEyeAuthError:
                errors["base"] = "invalid_auth"
            except (asyncio.TimeoutError, TimeoutError, OSError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during XMEye reconfigure")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        **extra,
                    },
                )

        current = reconfigure_entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=current[CONF_HOST]): str,
                vol.Optional(CONF_PORT, default=current[CONF_PORT]): vol.All(
                    int, vol.Range(min=1, max=65535)
                ),
                vol.Optional(CONF_USERNAME, default=current[CONF_USERNAME]): str,
                vol.Optional(CONF_PASSWORD, default=""): str,
            }),
            errors=errors,
            description_placeholders={"host": current[CONF_HOST]},
        )

    # ------------------------------------------------------------------
    # Reauth — recover from credential failure
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = reauth_entry.data[CONF_HOST]
            port = reauth_entry.data[CONF_PORT]
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            try:
                extra = await asyncio.wait_for(
                    _test_credentials(host, port, username, password), timeout=15.0
                )
            except XMEyeAuthError:
                errors["base"] = "invalid_auth"
            except (asyncio.TimeoutError, TimeoutError, OSError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during XMEye reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        **extra,
                    },
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, default=reauth_entry.data[CONF_USERNAME]): str,
                vol.Required(CONF_PASSWORD, default=""): str,
            }),
            errors=errors,
            description_placeholders={"host": reauth_entry.data[CONF_HOST]},
        )

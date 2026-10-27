"""Coordinator for official App cloud account (device list + IPC state)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud import TPLinkCloudAuthError, TPLinkCloudClient, TPLinkCloudError
from .cloud_helpers import (
    decode_alias,
    is_ipc_family,
    is_router_family,
    is_state_pollable,
    state_get_for_device,
    unwrap_passthrough,
)
from .const import CONF_CONNECTION_TYPE, CONN_LOCAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    try:
        return format_mac(str(mac).replace("-", ":"))
    except (ValueError, TypeError):
        cleaned = "".join(c for c in str(mac).lower() if c in "0123456789abcdef")
        if len(cleaned) == 12:
            return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
        return None


class TPLinkCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll getDeviceList; for online IPC/NVR also pull passthrough state."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TPLinkCloudClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_cloud",
            update_interval=timedelta(seconds=90),
        )
        self.client = client
        self.entry = entry

    def match_local_entry(self, mac: str | None) -> str | None:
        """Return config_entry_id of a local tplink_ipc entry with same MAC."""
        norm = normalize_mac(mac)
        if not norm:
            return None
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_CONNECTION_TYPE, CONN_LOCAL) != CONN_LOCAL:
                continue
            data = self.hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
            local = data.get("client")
            if local is None:
                continue
            local_mac = normalize_mac(getattr(local, "mac", None))
            if local_mac and local_mac == norm:
                return entry.entry_id
        return None

    async def _async_fetch_device_state(
        self, device_id: str, dev: dict[str, Any]
    ) -> dict[str, Any]:
        payload = state_get_for_device(dev)
        try:
            raw = await self.client.async_passthrough(device_id, payload)
        except TPLinkCloudError as err:
            _LOGGER.debug("Cloud state %s…: %s", device_id[-8:], err)
            return {"_error": str(err)}
        state = unwrap_passthrough(raw)
        if not isinstance(state, dict):
            return {"_error": "bad response"}
        if state.get("error_code") not in (0, None) and len(state) <= 2:
            return {"_error": state.get("msg") or str(state.get("error_code"))}
        return state

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            devices = await self.client.async_get_device_list()
        except TPLinkCloudAuthError:
            try:
                self.client.token = None
                await self.client.async_login()
                devices = await self.client.async_get_device_list()
            except TPLinkCloudError as err:
                raise UpdateFailed(str(err)) from err
        except TPLinkCloudError as err:
            raise UpdateFailed(str(err)) from err

        by_id: dict[str, dict[str, Any]] = {}
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            did = dev.get("deviceId")
            if not did:
                continue
            mac = normalize_mac(dev.get("deviceMac") or dev.get("mac"))
            enriched = {
                **dev,
                "mac_norm": mac,
                "matched_local_entry": self.match_local_entry(mac),
                "online": int(dev.get("status") or 0) == 1,
                "ipc_family": is_ipc_family(dev),
                "router_family": is_router_family(dev),
                "state": {},
            }
            by_id[str(did)] = enriched

        # Parallel passthrough for online IPC / doorbell / router devices
        jobs: list[tuple[str, Any]] = []
        for did, dev in by_id.items():
            if dev.get("online") and is_state_pollable(dev):
                jobs.append((did, self._async_fetch_device_state(did, dev)))

        if jobs:
            results = await asyncio.gather(
                *(coro for _, coro in jobs), return_exceptions=True
            )
            for (did, _), result in zip(jobs, results, strict=False):
                if isinstance(result, Exception):
                    by_id[did]["state"] = {"_error": str(result)}
                elif isinstance(result, dict):
                    by_id[did]["state"] = result
                    alias = decode_alias(dig_sys_alias(result))
                    if alias and not by_id[did].get("alias"):
                        by_id[did]["alias"] = alias

        online = sum(1 for d in by_id.values() if d.get("online"))
        ipc_n = sum(1 for d in by_id.values() if d.get("ipc_family"))
        router_n = sum(1 for d in by_id.values() if d.get("router_family"))
        return {
            "username": self.client.username,
            "devices": by_id,
            "count": len(by_id),
            "online_count": online,
            "ipc_count": ipc_n,
            "router_count": router_n,
        }


def dig_sys_alias(state: dict[str, Any]) -> Any:
    return (
        (state.get("system") or {}).get("sys", {}).get("dev_alias")
        or (state.get("system") or {}).get("clock_status", {}).get("dev_alias")
    )

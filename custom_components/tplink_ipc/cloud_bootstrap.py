"""Shared cloud client for local entries (media relay fallback).

When no cloud config entry exists, load credentials from known files so
``play_recording`` / ``stream_playback`` can still reach bizToken + relay.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .cloud import DEFAULT_APP_TYPE, DEFAULT_CLOUD_HOST, TPLinkCloudClient, TPLinkCloudError
from .cloud_coordinator import normalize_mac
from .cloud_relay import TPLinkCloudRelayClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SHARED_KEY = "_cloud_shared"


def _parse_env_creds(path: Path) -> tuple[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    user = pwd = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in ("CLOUD_USER", "username", "user"):
            user = val
        elif key in ("CLOUD_PASS", "password", "pass"):
            pwd = val
    if user and pwd:
        return user, pwd
    return None


def _cred_candidates(hass: HomeAssistant) -> list[Path]:
    return [
        Path(hass.config.path("tplink_ipc_cloud.env")),
        Path(hass.config.path("custom_components/tplink_ipc/.cloud_creds")),
    ]


def _session_terminal(hass: HomeAssistant) -> str | None:
    candidates = [
        Path(hass.config.path("tplink_ipc_cloud_session.json")),
    ]
    import json

    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        term = data.get("terminalUUID") or data.get("terminal_uuid")
        if term:
            return str(term)
    return None


async def async_ensure_shared_cloud(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return shared cloud bundle, creating it from creds file if needed."""
    hass.data.setdefault(DOMAIN, {})
    existing = hass.data[DOMAIN].get(SHARED_KEY)
    if isinstance(existing, dict) and existing.get("cloud_relay"):
        return existing

    # Prefer a real cloud config entry
    for data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(data, dict):
            continue
        if data.get("cloud_relay") and data.get("cloud_client"):
            return data

    creds = None
    cred_path = None
    for path in _cred_candidates(hass):
        creds = _parse_env_creds(path)
        if creds:
            cred_path = path
            break
    if not creds:
        _LOGGER.debug("No cloud creds file for shared media relay")
        return None

    username, password = creds
    terminal = _session_terminal(hass)
    cloud = TPLinkCloudClient(
        hass,
        username=username,
        password=password,
        terminal_uuid=terminal,
        app_type=DEFAULT_APP_TYPE,
        host=DEFAULT_CLOUD_HOST,
    )
    try:
        await cloud.async_login()
    except TPLinkCloudError as err:
        _LOGGER.warning("Shared cloud login failed (%s): %s", cred_path, err)
        return None

    relay = TPLinkCloudRelayClient(hass, cloud)
    devices_by_id: dict[str, dict[str, Any]] = {}
    try:
        devices = await cloud.async_get_device_list()
    except TPLinkCloudError as err:
        _LOGGER.warning("Shared cloud device list failed: %s", err)
        devices = []
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        did = dev.get("deviceId")
        if not did:
            continue
        mac = normalize_mac(dev.get("deviceMac") or dev.get("mac"))
        devices_by_id[str(did)] = {**dev, "mac_norm": mac}

    bundle = {
        "cloud_client": cloud,
        "cloud_relay": relay,
        "devices": devices_by_id,
        "source": f"creds:{cred_path}",
    }
    hass.data[DOMAIN][SHARED_KEY] = bundle
    _LOGGER.info(
        "Shared cloud media relay ready (%s devices, from %s)",
        len(devices_by_id),
        cred_path.name if cred_path else "?",
    )
    return bundle


def device_id_for_mac(bundle: dict[str, Any] | None, mac: str | None) -> str | None:
    norm = normalize_mac(mac)
    if not norm or not bundle:
        return None
    # Cloud coordinator shape
    devices = (bundle.get("cloud_coordinator") and bundle["cloud_coordinator"].data or {}).get(
        "devices"
    )
    if not devices:
        devices = bundle.get("devices") or {}
    for did, dev in devices.items():
        if normalize_mac(dev.get("mac_norm") or dev.get("deviceMac") or dev.get("mac")) == norm:
            return str(did)
    return None


def cloud_device(bundle: dict[str, Any] | None, device_id: str) -> dict[str, Any] | None:
    if not bundle:
        return None
    devices = (bundle.get("cloud_coordinator") and bundle["cloud_coordinator"].data or {}).get(
        "devices"
    )
    if not devices:
        devices = bundle.get("devices") or {}
    return devices.get(device_id)

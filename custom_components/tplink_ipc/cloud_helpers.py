"""Helpers for cloud device list + passthrough state."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

IPC_FAMILY_TYPES = {
    "SURVEILLANCECAMERA",
    "NETWORKVIDEORECORDER",
    "VIDEO.BATTERYDOORBELL",
    "DOORBELL",
}

ROUTER_FAMILY_TYPES = {
    "WIRELESSROUTER",
    "SMBROUTER",
    "ROUTER",
    "HOMEWIFISYSTEM",
}

# Camera / NVR / doorbell (stok-style)
CLOUD_STATE_GET_IPC = {
    "method": "get",
    "device_info": {"name": ["basic_info"]},
    "harddisk_manage": {"table": ["hd_info"], "name": ["harddisk"]},
    "record_plan": {"name": ["chn1_channel"]},
    "lens_mask": {"name": ["lens_mask_info"]},
    "led": {"name": ["config"]},
    "motion_detection": {"name": ["motion_det"]},
    "people_detection": {"name": ["detection"]},
    "network": {"name": ["wan_status"]},
    "video": {"name": ["main", "minor"]},
    "system": {"name": ["sys", "clock_status"]},
    "msg_push": {"name": ["chn1_msg_push_info"]},
    "msg_alarm": {"name": ["chn1_msg_alarm_info"]},
}

# Battery doorbell (VIDEO.BATTERYDOORBELL / TL-DB52C) — from APK libDeviceInfoListStorage
CLOUD_STATE_GET_DOORBELL = {
    "method": "get",
    "device_info": {"name": ["basic_info"]},
    "network": {"name": ["wan_status"]},
    "system": {"name": ["sys", "clock_status"]},
    "battery": {"name": ["setting"]},
    "low_power": {"name": ["power_mode"]},
    "doorbell_ring": {"name": ["wakeup_status"]},
    "pir_detection": {"name": ["pir_det"]},
    "doorbell_capability": {"name": ["ring"]},
    "battery_capability": {"name": ["capability"]},
    "sd_manage": {"table": ["sd_info"], "name": ["sd"]},
    "led": {"name": ["config"]},
    "lens_mask": {"name": ["lens_mask_info"]},
    "motion_detection": {"name": ["motion_det"]},
    "msg_push": {"name": ["chn1_msg_push_info"]},
    "msg_alarm": {"name": ["chn1_msg_alarm_info"]},
    "video": {"name": ["main", "minor"]},
}

# Home / SMB routers (XDR3030, R479GPE-AC, …)
CLOUD_STATE_GET_ROUTER = {
    "method": "get",
    "network": {"name": ["wan_status", "lan_status", "iface_mac"]},
    "hosts_info": {"table": ["host_info"]},
    "led": {"name": ["config"]},
    "system": {"name": ["sys", "clock_status"]},
    "device_info": {"name": ["basic_info"]},
    "function": {"name": ["module_spec"]},
}

# Back-compat alias
CLOUD_STATE_GET = CLOUD_STATE_GET_IPC


def device_type(dev: dict[str, Any]) -> str:
    return str(dev.get("deviceType") or dev.get("type") or "").upper()


def device_model(dev: dict[str, Any]) -> str:
    return str(dev.get("deviceModel") or dev.get("model") or "").upper()


def is_doorbell(dev: dict[str, Any]) -> bool:
    dt = device_type(dev)
    model = device_model(dev)
    return "DOORBELL" in dt or "DB52" in model


def is_ipc_family(dev: dict[str, Any]) -> bool:
    """Cameras, NVR, doorbells (IPC JSON control plane)."""
    if is_doorbell(dev):
        return True
    dt = device_type(dev)
    if dt in IPC_FAMILY_TYPES:
        return True
    return "CAMERA" in dt or "NVR" in dt


def is_router_family(dev: dict[str, Any]) -> bool:
    dt = device_type(dev)
    model = device_model(dev)
    if dt in ROUTER_FAMILY_TYPES or "ROUTER" in dt:
        return True
    return any(x in model for x in ("XDR", "R479", "ARCHER", "DECO"))


def is_state_pollable(dev: dict[str, Any]) -> bool:
    return is_ipc_family(dev) or is_router_family(dev)


def state_get_for_device(dev: dict[str, Any]) -> dict[str, Any]:
    if is_doorbell(dev):
        return CLOUD_STATE_GET_DOORBELL
    if is_router_family(dev):
        return CLOUD_STATE_GET_ROUTER
    return CLOUD_STATE_GET_IPC


def unwrap_passthrough(response: Any) -> dict[str, Any]:
    """Normalize passthrough responseData to a flat device JSON dict."""
    if not isinstance(response, dict):
        return {}
    if "responseData" in response and isinstance(response["responseData"], dict):
        return response["responseData"]
    return response


def dig(data: dict | None, path: tuple[str, ...]) -> Any:
    cur: Any = data or {}
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def first_hd_info(state: dict[str, Any]) -> dict[str, Any] | None:
    hd = dig(state, ("harddisk_manage", "hd_info"))
    if not isinstance(hd, list) or not hd:
        return None
    first = hd[0]
    if isinstance(first, dict):
        for val in first.values():
            if isinstance(val, dict):
                return val
    return None


def wan_status(state: dict[str, Any]) -> dict[str, Any]:
    wan = dig(state, ("network", "wan_status"))
    return wan if isinstance(wan, dict) else {}


def iter_hosts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten hosts_info.host_info entries."""
    raw = dig(state, ("hosts_info", "host_info"))
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        for val in item.values():
            if isinstance(val, dict) and (val.get("ip") or val.get("mac")):
                out.append(val)
    return out


def battery_info(state: dict[str, Any]) -> dict[str, Any]:
    """Best-effort battery dict from doorbell / power modules."""
    for path in (
        ("battery", "setting"),  # APK: get battery.name=setting
        ("battery", "info"),
        ("battery", "status"),
        ("battery", "basic_info"),
        ("battery", "battery_info"),
        ("power", "battery"),
        ("power", "status"),
        ("low_power", "power_mode"),
    ):
        val = dig(state, path)
        if isinstance(val, dict) and val:
            if any(
                v not in (None, "", [], {})
                for k, v in val.items()
                if not str(k).startswith(".")
            ):
                return val
    bat = state.get("battery")
    if isinstance(bat, dict):
        return bat
    return {}


def battery_percent(state: dict[str, Any]) -> float | None:
    info = battery_info(state)
    for key in (
        "percent",
        "battery_percent",
        "battery_percentage",
        "percentage",
        "residual",
        "capacity",
        "level",
        "power",
        "battery_display_num",
    ):
        if info.get(key) is not None:
            try:
                return float(info[key])
            except (TypeError, ValueError):
                continue
    return None


def decode_alias(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return unquote(str(value))
    except Exception:  # noqa: BLE001
        return str(value)


def icon_for_type(dev: dict[str, Any]) -> str:
    if is_doorbell(dev):
        return "mdi:doorbell-video"
    if is_router_family(dev):
        model = device_model(dev)
        if "XDR" in model or "MESH" in model:
            return "mdi:router-wireless"
        return "mdi:router-network"
    dt = device_type(dev)
    if "NVR" in dt or "RECORDER" in dt:
        return "mdi:harddisk"
    if "CAMERA" in dt:
        return "mdi:cctv"
    return "mdi:devices"

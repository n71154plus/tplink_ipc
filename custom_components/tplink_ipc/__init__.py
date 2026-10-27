"""TP-Link China IPC — full local API coverage from tpSurveillance APK."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from homeassistant.exceptions import HomeAssistantError

from .api import TPLinkIPCClient, TPLinkIPCError
from .cloud import (
    DEFAULT_APP_TYPE,
    DEFAULT_CLOUD_HOST,
    TPLinkCloudClient,
    TPLinkCloudError,
)
from .cloud_bootstrap import (
    async_ensure_shared_cloud,
    cloud_device,
    device_id_for_mac,
)
from .cloud_coordinator import TPLinkCloudCoordinator, normalize_mac
from .cloud_entity import async_register_cloud_account_device
from .cloud_relay import TPLinkCloudRelayClient, TPLinkCloudRelayError
from .const import (
    ATTR_BIZ_SERVICE_ID,
    ATTR_CHANNEL,
    ATTR_CLIENT_ID,
    ATTR_DATE,
    ATTR_DEVICE_ID,
    ATTR_DIRECTION,
    ATTR_END_DATE,
    ATTR_END_INDEX,
    ATTR_END_TIME,
    ATTR_MAX_BYTES,
    ATTR_MAX_NUM,
    ATTR_MAX_SECONDS,
    ATTR_PAN,
    ATTR_PAYLOAD,
    ATTR_PRESET_ID,
    ATTR_PRESET_NAME,
    ATTR_SEGMENT_INDEX,
    ATTR_START_DATE,
    ATTR_START_INDEX,
    ATTR_START_TIME,
    ATTR_STREAM_TYPE,
    ATTR_TILT,
    ATTR_X,
    ATTR_Y,
    ATTR_ZOOM,
    CLOUD_PLATFORMS,
    CONF_APP_TYPE,
    CONF_CLOUD_HOST,
    CONF_CONNECTION_TYPE,
    CONF_RTSP_PORT,
    CONF_STREAM_PATH,
    CONF_TERMINAL_UUID,
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    CONN_CLOUD,
    CONN_LOCAL,
    DEFAULT_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_STREAM_PATH,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    PLAYBACK_BRIDGE_PORT,
    PLATFORMS,
    playback_stream_name,
    SERVICE_CLOUD_DEVICE_LIST,
    SERVICE_CLOUD_GET_BIZ_TOKEN,
    SERVICE_CLOUD_GET_DEVICE_INFO,
    SERVICE_CLOUD_GET_DISK,
    SERVICE_CLOUD_GET_RECORD_PLAN,
    SERVICE_CLOUD_GET_RELAY_CLUSTER,
    SERVICE_CLOUD_PASSTHROUGH,
    SERVICE_CLOUD_PLAY_RECORDING,
    SERVICE_CLOUD_PREPARE_RELAY,
    SERVICE_CLOUD_REBOOT,
    SERVICE_DOORBELL_WAKEUP,
    SERVICE_FORMAT_DISK,
    SERVICE_FORMAT_SD,
    SERVICE_GET_FORMAT_STATUS,
    SERVICE_PLAY_RECORDING,
    SERVICE_PTZ_CALIBRATE,
    SERVICE_PTZ_CONTINUOUS,
    SERVICE_PTZ_MOVE,
    SERVICE_PTZ_PRESET,
    SERVICE_PTZ_SET_PRESET,
    SERVICE_PTZ_STEP,
    SERVICE_PTZ_STOP,
    SERVICE_PTZ_ZOOM,
    SERVICE_RAW,
    SERVICE_REBOOT,
    SERVICE_REFRESH_CALENDARS,
    SERVICE_SEARCH_DAYS,
    SERVICE_SEARCH_EVENTS,
    SERVICE_SEARCH_TIMELINE,
    SERVICE_SEARCH_VIDEO,
    SERVICE_SET_TIMING_REBOOT,
    SERVICE_STREAM_PLAYBACK,
    SERVICE_TRIGGER_ALARM,
    ATTR_DISK_NAME,
    ATTR_SD_NAME,
    ATTR_DAY,
    ATTR_TIME,
    ATTR_ENABLED,
    ATTR_SECONDS,
)
from .coordinator import TPLinkIPCCoordinator
from .go2rtc_api import Go2rtcError, patch_stream
from .playback_bridge import PlaybackBridge, PlaybackJob
from .stream_session import TPLinkMediaStreamError

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    conn = data.get(CONF_CONNECTION_TYPE, CONN_LOCAL)
    hass.data.setdefault(DOMAIN, {})

    if conn == CONN_CLOUD:
        cloud = TPLinkCloudClient(
            hass,
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            terminal_uuid=data.get(CONF_TERMINAL_UUID),
            app_type=data.get(CONF_APP_TYPE, DEFAULT_APP_TYPE),
            host=data.get(CONF_CLOUD_HOST, DEFAULT_CLOUD_HOST),
        )
        try:
            await cloud.async_login()
        except TPLinkCloudError:
            _LOGGER.exception("Cloud login failed for entry %s", entry.title)
            raise
        cloud_coord = TPLinkCloudCoordinator(hass, cloud, entry)
        await cloud_coord.async_config_entry_first_refresh()
        relay = TPLinkCloudRelayClient(hass, cloud)
        hass.data[DOMAIN][entry.entry_id] = {
            "cloud_client": cloud,
            "cloud_coordinator": cloud_coord,
            "cloud_relay": relay,
        }
        async_register_cloud_account_device(
            hass, entry, username=cloud.username
        )
        await hass.config_entries.async_forward_entry_setups(entry, CLOUD_PLATFORMS)
        _async_register_services(hass)
        return True

    client = TPLinkIPCClient(
        hass,
        host=data[CONF_HOST],
        port=data.get(CONF_PORT, DEFAULT_PORT),
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        use_https=data.get(CONF_USE_HTTPS, False),
        verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        rtsp_port=data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        stream_path=data.get(CONF_STREAM_PATH, DEFAULT_STREAM_PATH),
    )
    coordinator = TPLinkIPCCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_ensure_playback_bridge(hass, restart=True)
    _async_register_services(hass)
    return True


async def _async_ensure_playback_bridge(
    hass: HomeAssistant, *, restart: bool = False
) -> PlaybackBridge:
    bridge = hass.data[DOMAIN].get("_playback_bridge")
    if isinstance(bridge, PlaybackBridge):
        if not restart:
            return bridge
        try:
            await bridge.async_stop()
        except Exception:  # noqa: BLE001
            pass
        hass.data[DOMAIN].pop("_playback_bridge", None)
    bridge = PlaybackBridge(port=PLAYBACK_BRIDGE_PORT)
    await bridge.async_start()
    hass.data[DOMAIN]["_playback_bridge"] = bridge
    return bridge


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    conn = entry.data.get(CONF_CONNECTION_TYPE, CONN_LOCAL)
    platforms = CLOUD_PLATFORMS if conn == CONN_CLOUD else PLATFORMS
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


def _async_register_services(hass: HomeAssistant) -> None:
    register_local = not hass.services.has_service(DOMAIN, SERVICE_PTZ_MOVE)
    register_cloud = not hass.services.has_service(DOMAIN, SERVICE_CLOUD_DEVICE_LIST)
    register_cloud_ux = not hass.services.has_service(DOMAIN, SERVICE_CLOUD_REBOOT)
    register_cloud_media = not hass.services.has_service(
        DOMAIN, SERVICE_CLOUD_GET_BIZ_TOKEN
    )
    # Always (re)bind stream_playback — config-entry reload must pick up handler fixes
    rebind_stream = hass.services.has_service(DOMAIN, SERVICE_STREAM_PLAYBACK)
    if (
        not register_local
        and not register_cloud
        and not register_cloud_ux
        and not register_cloud_media
        and not rebind_stream
    ):
        return

    async def _client(call: ServiceCall) -> TPLinkIPCClient:
        from homeassistant.helpers import entity_registry as er

        entity_id = call.data.get("entity_id")
        entry_id = None
        if entity_id:
            ent = er.async_get(hass).async_get(entity_id)
            if ent:
                entry_id = ent.config_entry_id
        if not entry_id:
            for eid, data in hass.data.get(DOMAIN, {}).items():
                if data.get("client") is not None:
                    entry_id = eid
                    break
        if not entry_id or "client" not in hass.data[DOMAIN].get(entry_id, {}):
            raise HomeAssistantError("No local tplink_ipc device configured")
        return hass.data[DOMAIN][entry_id]["client"]

    async def _cloud(call: ServiceCall) -> TPLinkCloudClient:
        for data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(data, dict):
                continue
            cloud = data.get("cloud_client")
            if cloud is not None:
                return cloud
        shared = await async_ensure_shared_cloud(hass)
        if shared and shared.get("cloud_client"):
            return shared["cloud_client"]
        raise HomeAssistantError(
            "尚未設定 TP-Link 雲端帳號（整合 → 新增 → 雲端帳號，"
            "或放置 config/tplink_ipc_cloud.env）"
        )

    async def _cloud_bundle(call: ServiceCall | None = None) -> dict:
        for data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(data, dict):
                continue
            if data.get("cloud_relay") and data.get("cloud_client"):
                return data
        shared = await async_ensure_shared_cloud(hass)
        if shared:
            return shared
        raise HomeAssistantError(
            "尚未設定 TP-Link 雲端帳號（整合 → 新增 → 雲端帳號，"
            "或放置 config/tplink_ipc_cloud.env）"
        )

    def _channel_from_call(call: ServiceCall) -> int:
        """Prefer explicit channel; else read playback_channel from calendar entity."""
        if ATTR_CHANNEL in call.data and call.data[ATTR_CHANNEL] is not None:
            return int(call.data[ATTR_CHANNEL])
        entity_id = call.data.get("entity_id")
        if entity_id:
            state = hass.states.get(entity_id)
            if state is not None:
                ch = state.attributes.get("playback_channel")
                if ch is not None:
                    return int(ch)
        return 0

    async def handle_cloud_device_list(call: ServiceCall) -> dict:
        cloud = await _cloud(call)
        try:
            devices = await cloud.async_get_device_list()
        except TPLinkCloudError as err:
            return {"ok": False, "error": str(err), "devices": []}
        return {
            "ok": True,
            "username": cloud.username,
            "count": len(devices),
            "devices": devices,
        }

    async def handle_cloud_passthrough(call: ServiceCall) -> dict:
        cloud = await _cloud(call)
        device_id = call.data[ATTR_DEVICE_ID]
        payload = call.data[ATTR_PAYLOAD]
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be a JSON object"}
        try:
            response = await cloud.async_passthrough(device_id, payload)
        except TPLinkCloudError as err:
            return {"ok": False, "error": str(err), "device_id": device_id}
        return {"ok": True, "device_id": device_id, "response": response}

    async def handle_cloud_reboot(call: ServiceCall) -> dict:
        cloud = await _cloud(call)
        device_id = call.data[ATTR_DEVICE_ID]
        try:
            response = await cloud.async_passthrough(
                device_id, {"method": "do", "system": {"reboot": None}}
            )
        except TPLinkCloudError as err:
            return {"ok": False, "error": str(err), "device_id": device_id}
        return {"ok": True, "device_id": device_id, "response": response}

    async def handle_cloud_get_disk(call: ServiceCall) -> dict:
        cloud = await _cloud(call)
        device_id = call.data[ATTR_DEVICE_ID]
        try:
            response = await cloud.async_passthrough(
                device_id,
                {
                    "method": "get",
                    "harddisk_manage": {
                        "table": ["hd_info"],
                        "name": ["harddisk"],
                    },
                },
            )
        except TPLinkCloudError as err:
            return {"ok": False, "error": str(err), "device_id": device_id}
        return {"ok": True, "device_id": device_id, "response": response}

    async def handle_cloud_get_record_plan(call: ServiceCall) -> dict:
        cloud = await _cloud(call)
        device_id = call.data[ATTR_DEVICE_ID]
        try:
            response = await cloud.async_passthrough(
                device_id,
                {"method": "get", "record_plan": {"name": ["chn1_channel"]}},
            )
        except TPLinkCloudError as err:
            return {"ok": False, "error": str(err), "device_id": device_id}
        return {"ok": True, "device_id": device_id, "response": response}

    async def handle_cloud_get_device_info(call: ServiceCall) -> dict:
        cloud = await _cloud(call)
        device_id = call.data[ATTR_DEVICE_ID]
        try:
            response = await cloud.async_passthrough(
                device_id,
                {"method": "get", "device_info": {"name": ["basic_info"]}},
            )
        except TPLinkCloudError as err:
            return {"ok": False, "error": str(err), "device_id": device_id}
        return {"ok": True, "device_id": device_id, "response": response}

    async def handle_cloud_get_biz_token(call: ServiceCall) -> dict:
        bundle = await _cloud_bundle(call)
        relay: TPLinkCloudRelayClient = bundle["cloud_relay"]
        sid = call.data.get(ATTR_BIZ_SERVICE_ID)
        try:
            token = await relay.async_get_biz_token(biz_service_id=sid)
        except TPLinkCloudRelayError as err:
            return {"ok": False, "error": str(err), "raw": getattr(err, "raw", None)}
        return {
            "ok": True,
            "bizToken": token,
            "bizTokenMedia": relay.biz_token_media,
            "bizTokenApp": relay.biz_token_app,
        }

    async def handle_cloud_get_relay_cluster(call: ServiceCall) -> dict:
        bundle = await _cloud_bundle(call)
        relay: TPLinkCloudRelayClient = bundle["cloud_relay"]
        device_id = call.data[ATTR_DEVICE_ID]
        device = cloud_device(bundle, device_id) or {"deviceId": device_id}
        try:
            cluster = await relay.async_get_relay_cluster_address(device)
        except TPLinkCloudRelayError as err:
            return {"ok": False, "error": str(err), "device_id": device_id, "raw": getattr(err, "raw", None)}
        return {
            "ok": True,
            "device_id": device_id,
            "cluster": cluster,
            "relay_host": relay.relay_host,
            "relayMasterUrl": relay.relay_master_url,
        }

    async def handle_cloud_prepare_relay(call: ServiceCall) -> dict:
        bundle = await _cloud_bundle(call)
        relay: TPLinkCloudRelayClient = bundle["cloud_relay"]
        device_id = call.data[ATTR_DEVICE_ID]
        device = cloud_device(bundle, device_id) or {"deviceId": device_id}
        stream_type = str(call.data.get(ATTR_STREAM_TYPE, "sdvod"))
        channel = int(call.data.get(ATTR_CHANNEL, 0))
        try:
            result = await relay.async_prepare_media_session(
                device, stream_type=stream_type, channel_id=channel
            )
        except TPLinkCloudRelayError as err:
            return {
                "ok": False,
                "error": str(err),
                "device_id": device_id,
                "raw": getattr(err, "raw", None),
            }
        result["device_id"] = device_id
        return result

    async def handle_cloud_play_recording(call: ServiceCall) -> dict:
        """Download a clip via cloud relayservice (for cameras without Streamd)."""
        bundle = await _cloud_bundle(call)
        relay: TPLinkCloudRelayClient = bundle["cloud_relay"]
        device_id = call.data[ATTR_DEVICE_ID]
        device = cloud_device(bundle, device_id) or {"deviceId": device_id}
        start_time = call.data.get(ATTR_START_TIME)
        end_time = call.data.get(ATTR_END_TIME)
        if start_time is None or end_time is None:
            return {"ok": False, "error": "start_time and end_time required"}
        channel = int(call.data.get(ATTR_CHANNEL, 0))
        max_seconds = float(call.data.get(ATTR_MAX_SECONDS, 30))
        max_bytes = int(call.data.get(ATTR_MAX_BYTES, 12_000_000))
        stream_type = str(call.data.get(ATTR_STREAM_TYPE, "sdvod"))
        try:
            ts_data, meta = await relay.async_download_playback(
                device_id,
                start_time,
                end_time,
                channel=channel,
                max_bytes=max_bytes,
                max_seconds=max_seconds + 10,
                stream_type=stream_type,
                device=device,
            )
        except TPLinkCloudRelayError as err:
            return {
                "ok": False,
                "error": str(err),
                "device_id": device_id,
                "source": "cloud_relay",
                "raw": getattr(err, "raw", None),
            }
        www = Path(hass.config.path("www", "tplink_ipc"))
        www.mkdir(parents=True, exist_ok=True)
        stem = f"cloud_{device_id[-8:]}_{start_time}_{end_time}"
        ts_path = www / f"{stem}.ts"
        ts_path.write_bytes(ts_data)
        return {
            "ok": True,
            "device_id": device_id,
            "source": "cloud_relay",
            "bytes": len(ts_data),
            "path": f"/local/tplink_ipc/{ts_path.name}",
            "meta": meta,
            "note": (
                "若 relayservice 仍 400/401，需先打通 relayMaster"
                "（見 cloud_prepare_relay）"
            ),
        }

    async def _cloud_device_for_local(
        client: TPLinkIPCClient,
    ) -> tuple[str | None, dict | None, TPLinkCloudRelayClient | None]:
        """Resolve cloud deviceId + device dict + relay for a local client."""
        mac = normalize_mac(getattr(client, "mac", None))
        bundle = None
        for data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(data, dict):
                continue
            if data.get("cloud_relay"):
                bundle = data
                break
        if bundle is None:
            bundle = await async_ensure_shared_cloud(hass)
        if not bundle:
            return None, None, None
        relay = bundle.get("cloud_relay")
        device_id = device_id_for_mac(bundle, mac)
        # Also search coordinator devices if present
        if not device_id:
            for data in hass.data.get(DOMAIN, {}).values():
                if not isinstance(data, dict):
                    continue
                coord = data.get("cloud_coordinator")
                if coord is None or not coord.data:
                    continue
                for did, dev in (coord.data.get("devices") or {}).items():
                    if normalize_mac(dev.get("mac_norm") or dev.get("deviceMac")) == mac:
                        device_id = did
                        break
        device = cloud_device(bundle, device_id) if device_id else None
        if device_id and device is None:
            # Minimal stub so cluster call still has deviceId
            device = {"deviceId": device_id}
            for data in hass.data.get(DOMAIN, {}).values():
                if not isinstance(data, dict):
                    continue
                coord = data.get("cloud_coordinator")
                if coord and coord.data:
                    device = (coord.data.get("devices") or {}).get(device_id) or device
                    break
            if device == {"deviceId": device_id} and bundle.get("devices"):
                device = bundle["devices"].get(device_id) or device
        return device_id, device, relay

    def _cloud_device_id_for_local(client: TPLinkIPCClient) -> str | None:
        mac = normalize_mac(getattr(client, "mac", None))
        if not mac:
            return None
        for data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(data, dict):
                continue
            coord = data.get("cloud_coordinator")
            if coord is None or not coord.data:
                continue
            for did, dev in (coord.data.get("devices") or {}).items():
                if normalize_mac(dev.get("mac_norm") or dev.get("deviceMac")) == mac:
                    return did
            shared = data.get("devices")
            if shared:
                for did, dev in shared.items():
                    if normalize_mac(dev.get("mac_norm") or dev.get("deviceMac")) == mac:
                        return did
        shared = hass.data.get(DOMAIN, {}).get("_cloud_shared")
        if isinstance(shared, dict):
            return device_id_for_mac(shared, mac)
        return None

    def _cloud_relay() -> TPLinkCloudRelayClient | None:
        for data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(data, dict):
                continue
            relay = data.get("cloud_relay")
            if relay is not None:
                return relay
        return None

    async def _async_cloud_relay() -> TPLinkCloudRelayClient | None:
        relay = _cloud_relay()
        if relay is not None:
            return relay
        shared = await async_ensure_shared_cloud(hass)
        if shared:
            return shared.get("cloud_relay")
        return None

    async def handle_ptz_move(call: ServiceCall) -> None:
        await (await _client(call)).ptz_move(call.data[ATTR_X], call.data[ATTR_Y])

    async def handle_ptz_step(call: ServiceCall) -> None:
        await (await _client(call)).ptz_step(call.data[ATTR_DIRECTION])

    async def handle_ptz_stop(call: ServiceCall) -> None:
        await (await _client(call)).ptz_stop()

    async def handle_ptz_continuous(call: ServiceCall) -> None:
        await (await _client(call)).ptz_continuous(
            pan=float(call.data.get(ATTR_PAN, 0)),
            tilt=float(call.data.get(ATTR_TILT, 0)),
            zoom=float(call.data.get(ATTR_ZOOM, 0)),
        )

    async def handle_ptz_zoom(call: ServiceCall) -> None:
        await (await _client(call)).ptz_zoom(call.data[ATTR_ZOOM])

    async def handle_ptz_calibrate(call: ServiceCall) -> None:
        await (await _client(call)).ptz_calibrate()

    async def handle_ptz_preset(call: ServiceCall) -> None:
        await (await _client(call)).ptz_goto_preset(call.data[ATTR_PRESET_ID])

    async def handle_ptz_set_preset(call: ServiceCall) -> None:
        await (await _client(call)).ptz_set_preset(call.data[ATTR_PRESET_NAME])

    async def handle_reboot(call: ServiceCall) -> None:
        await (await _client(call)).reboot()

    async def handle_format_disk(call: ServiceCall) -> dict:
        client = await _client(call)
        disk = str(call.data.get(ATTR_DISK_NAME, "1"))
        result = await client.format_disk(disk)
        return {"ok": result.get("error_code", -1) in (0, None), "result": result, "disk_name": disk}

    async def handle_format_sd(call: ServiceCall) -> dict:
        client = await _client(call)
        name = str(call.data.get(ATTR_SD_NAME, "1"))
        result = await client.format_sd(name)
        return {"ok": result.get("error_code", -1) in (0, None), "result": result, "sd_name": name}

    async def handle_get_format_status(call: ServiceCall) -> dict:
        client = await _client(call)
        disk = str(call.data.get(ATTR_DISK_NAME, "1"))
        result = await client.get_format_status(disk)
        return {"ok": result.get("error_code", -1) in (0, None), "result": result, "disk_name": disk}

    async def handle_set_timing_reboot(call: ServiceCall) -> dict:
        client = await _client(call)
        enabled = call.data.get(ATTR_ENABLED)
        if enabled is not None and not isinstance(enabled, bool):
            enabled = str(enabled).lower() in ("on", "1", "true", "yes")
        result = await client.set_timing_reboot(
            enabled=enabled,
            day=call.data.get(ATTR_DAY),
            time_hm=call.data.get(ATTR_TIME),
        )
        return {"ok": result.get("error_code", -1) in (0, None), "result": result}

    async def handle_doorbell_wakeup(call: ServiceCall) -> dict:
        client = await _client(call)
        result = await client.doorbell_wakeup()
        return {"ok": result.get("error_code", -1) in (0, None), "result": result}

    async def handle_trigger_alarm(call: ServiceCall) -> None:
        client = await _client(call)
        await client.trigger_alarm(seconds=float(call.data.get(ATTR_SECONDS, 5)))

    async def handle_raw(call: ServiceCall) -> None:
        client = await _client(call)
        payload = call.data["payload"]
        result = await client.request(payload, raise_error=False)
        _LOGGER.info("tplink_ipc raw_request result: %s", result)
        hass.bus.async_fire(
            f"{DOMAIN}_raw_response",
            {"result": result, "host": client.host},
        )

    async def handle_search_days(call: ServiceCall) -> dict:
        client = await _client(call)
        try:
            days = await client.search_recording_days(
                call.data[ATTR_START_DATE],
                call.data[ATTR_END_DATE],
                channel=int(call.data.get(ATTR_CHANNEL, 0)),
            )
        except TPLinkIPCError as err:
            for data in hass.data[DOMAIN].values():
                if data.get("client") is client:
                    coordinator = data.get("coordinator")
                    if coordinator:
                        coordinator.recording_days_error = str(err)
                        coordinator.async_set_updated_data(coordinator.data or {})
                    break
            return {"ok": False, "host": client.host, "error": str(err), "days": []}
        payload = {
            "ok": True,
            "host": client.host,
            "name": client.device_name,
            "playback_version": client.playback_version,
            "days": days,
            "count": len(days),
        }
        for data in hass.data[DOMAIN].values():
            if data.get("client") is client:
                coordinator = data.get("coordinator")
                if coordinator:
                    coordinator.recording_days = days
                    coordinator.recording_days_error = None
                    coordinator.async_set_updated_data(coordinator.data or {})
                break
        hass.bus.async_fire(f"{DOMAIN}_recording_days", payload)
        return payload

    async def handle_search_video(call: ServiceCall) -> dict:
        client = await _client(call)
        try:
            segments = await client.search_video_segments(
                call.data[ATTR_DATE],
                channel=int(call.data.get(ATTR_CHANNEL, 0)),
                start_index=int(call.data.get(ATTR_START_INDEX, 0)),
                end_index=int(call.data.get(ATTR_END_INDEX, 99)),
            )
        except TPLinkIPCError as err:
            return {
                "ok": False,
                "host": client.host,
                "error": str(err),
                "segments": [],
            }
        payload = {
            "ok": True,
            "host": client.host,
            "date": TPLinkIPCClient._ymd(call.data[ATTR_DATE]),
            "channel": int(call.data.get(ATTR_CHANNEL, 0)),
            "segments": segments,
            "count": len(segments),
        }
        for data in hass.data[DOMAIN].values():
            if data.get("client") is client:
                coordinator = data.get("coordinator")
                if coordinator:
                    tl = {**payload, "source": "search_video_utility"}
                    ch = int(payload.get("channel") or 0)
                    coordinator.last_timeline_by_channel[ch] = tl
                    coordinator.last_timeline = tl
                    coordinator.async_set_updated_data(coordinator.data or {})
                break
        hass.bus.async_fire(f"{DOMAIN}_video_segments", payload)
        return payload

    async def handle_search_timeline(call: ServiceCall) -> dict:
        client = await _client(call)
        channel = _channel_from_call(call)
        for data in hass.data[DOMAIN].values():
            if data.get("client") is client:
                coordinator = data.get("coordinator")
                if coordinator:
                    return await coordinator.async_load_timeline(
                        call.data[ATTR_DATE], channel=channel
                    )
        return await client.search_day_timeline(
            call.data[ATTR_DATE], channel=channel
        )

    async def handle_refresh_calendars(call: ServiceCall) -> dict:
        results = []
        targets = list(hass.data[DOMAIN].values())
        entity_id = call.data.get("entity_id")
        if entity_id:
            from homeassistant.helpers import entity_registry as er

            ent = er.async_get(hass).async_get(entity_id)
            if ent and ent.config_entry_id in hass.data[DOMAIN]:
                targets = [hass.data[DOMAIN][ent.config_entry_id]]
        for data in targets:
            coordinator = data.get("coordinator")
            client = data.get("client")
            if not coordinator or not client:
                continue
            try:
                days_map = await coordinator.async_refresh_recording_days(force=True)
                coordinator.async_set_updated_data(coordinator.data or {})
                results.append(
                    {
                        "host": client.host,
                        "name": client.device_name,
                        "ok": True,
                        "channels": {
                            str(k): len(v) for k, v in days_map.items()
                        },
                    }
                )
            except Exception as err:  # noqa: BLE001
                results.append(
                    {"host": client.host, "ok": False, "error": str(err)}
                )
        return {"ok": True, "devices": results}

    async def handle_search_events(call: ServiceCall) -> dict:
        client = await _client(call)
        try:
            events = await client.search_events(
                call.data[ATTR_START_TIME],
                call.data[ATTR_END_TIME],
                channel=int(call.data.get(ATTR_CHANNEL, 0)),
                start_index=int(call.data.get(ATTR_START_INDEX, 0)),
                max_num=int(call.data.get(ATTR_MAX_NUM, 100)),
                client_id=int(call.data.get(ATTR_CLIENT_ID, 2)),
            )
        except TPLinkIPCError as err:
            return {
                "ok": False,
                "host": client.host,
                "error": str(err),
                "events": [],
                "playback_version": client.playback_version,
            }
        payload = {
            "ok": True,
            "host": client.host,
            "events": events,
            "count": len(events),
            "playback_version": client.playback_version,
        }
        hass.bus.async_fire(f"{DOMAIN}_events", payload)
        return payload

    async def _resolve_clip_window(
        client: TPLinkIPCClient, call: ServiceCall, channel: int
    ) -> dict:
        """Resolve clip window.

        Success: {ok, start_time, end_time, ...}
        Failure: {ok: False, error, ...}

        For synthetic hourly slots (IPC without segment list), probe Streamd
        and skip empty hours (error_code=0 but 0 MPEG-TS) so 「最近錄影」
        lands on a window that actually plays.
        """
        from functools import partial

        from .stream_session import playback_has_mpegts

        start_time = call.data.get(ATTR_START_TIME)
        end_time = call.data.get(ATTR_END_TIME)
        max_seconds = float(call.data.get(ATTR_MAX_SECONDS, 30))
        picked_idx: int | None = None
        timeline: dict | None = None
        skipped_empty: list[dict] = []
        preferred_port: int | None = None
        preferred_use_stok: bool | None = None
        from_timeline = start_time is None or end_time is None

        if from_timeline:
            date = call.data.get(ATTR_DATE)
            if not date:
                from homeassistant.util import dt as dt_util

                date = dt_util.now().strftime("%Y%m%d")
            timeline = await client.search_day_timeline(date, channel=channel)
            segments = list(timeline.get("segments") or [])
            if not timeline.get("ok") or not segments:
                return {
                    "ok": False,
                    "error": timeline.get("error")
                    or "no segments for date/channel",
                    "timeline": timeline,
                }
            idx = int(call.data.get(ATTR_SEGMENT_INDEX, 0))
            idx = max(0, min(idx, len(segments) - 1))
            synthetic = timeline.get("source") == "synthetic_slots"
            # Probe / skip a few candidates (empty synthetic hours, or
            # NVR segments that just closed and are still flushing).
            candidates = list(range(idx, min(idx + 4, len(segments))))
            stream_port = int(getattr(client, "stream_port", 8800) or 8800)
            if synthetic:
                try:
                    await client.login()
                except (TPLinkIPCError, TimeoutError, OSError) as err:
                    return {
                        "ok": False,
                        "error": f"login for playback probe failed: {err}",
                    }
                assert client._stok

            import time as _time

            now_ts = int(_time.time())
            for cand in candidates:
                seg = segments[cand]
                st = seg.get("startTime") or seg.get("start_time")
                et = seg.get("endTime") or seg.get("end_time")
                if st is None or et is None:
                    continue
                try:
                    st_i, et_i = int(st), int(et)
                except (TypeError, ValueError):
                    continue
                # Skip clips that ended <90s ago — NVR Streamd often stalls
                # on the still-open / just-flushed file (TTFB 15–20s+).
                if not synthetic and now_ts - et_i < 90:
                    skipped_empty.append(
                        {
                            "index": cand,
                            "start_time": str(st),
                            "end_time": str(et),
                            "reason": "segment_too_fresh",
                        }
                    )
                    continue
                if not synthetic:
                    picked_idx = cand
                    start_time, end_time = st, et
                    break
                probe_end = min(et_i, st_i + 90)
                probe = partial(
                    playback_has_mpegts,
                    client.host,
                    client.username,
                    client.password,
                    client._stok,
                    st,
                    probe_end,
                    channel=channel,
                    stream_port=stream_port,
                    max_seconds=1.5,
                )
                try:
                    ok, nbytes, probe_meta = await hass.async_add_executor_job(probe)
                except (TPLinkMediaStreamError, OSError, TimeoutError) as err:
                    skipped_empty.append(
                        {
                            "index": cand,
                            "start_time": str(st),
                            "label": seg.get("label"),
                            "probe_bytes": 0,
                            "error": str(err),
                        }
                    )
                    continue
                if ok:
                    picked_idx = cand
                    start_time, end_time = st, et
                    if isinstance(probe_meta, dict):
                        if probe_meta.get("stream_port"):
                            preferred_port = int(probe_meta["stream_port"])
                        if "use_stok_path" in probe_meta:
                            preferred_use_stok = bool(probe_meta["use_stok_path"])
                    break
                skipped_empty.append(
                    {
                        "index": cand,
                        "start_time": str(st),
                        "label": seg.get("label"),
                        "probe_bytes": nbytes,
                    }
                )
            if start_time is None or end_time is None:
                return {
                    "ok": False,
                    "error": (
                        "no playable clip nearby "
                        "(empty hour or segment still flushing)"
                    ),
                    "timeline": timeline,
                    "skipped_empty": skipped_empty,
                }

        else:
            # Explicit clock time from UI — snap onto real footage first.
            # Otherwise Streamd often returns error_code=0 with 0 bytes and the
            # bridge/go2rtc sit idle ~15–20s before the picture ever appears.
            import time as _time

            try:
                want = int(start_time)
                want_end = int(end_time)
            except (TypeError, ValueError):
                want = None
                want_end = None
            if want is not None:
                day = _time.strftime("%Y%m%d", _time.gmtime(want + 8 * 3600))
                # Reuse coordinator timeline when already loaded for this day.
                timeline = None
                for data in hass.data.get(DOMAIN, {}).values():
                    if not isinstance(data, dict) or data.get("client") is not client:
                        continue
                    coord = data.get("coordinator")
                    if not coord:
                        continue
                    cached = (getattr(coord, "last_timeline_by_channel", {}) or {}).get(
                        int(channel)
                    ) or {}
                    if (
                        cached.get("ok")
                        and str(cached.get("date") or "") == day
                        and cached.get("segments")
                    ):
                        timeline = cached
                    break
                if timeline is None:
                    timeline = await client.search_day_timeline(
                        day, channel=channel
                    )
                    for data in hass.data.get(DOMAIN, {}).values():
                        if (
                            isinstance(data, dict)
                            and data.get("client") is client
                            and data.get("coordinator")
                        ):
                            data["coordinator"].last_timeline_by_channel[
                                int(channel)
                            ] = timeline
                            data["coordinator"].last_timeline = timeline
                            break
                segments = list(timeline.get("segments") or [])
                synthetic = timeline.get("source") == "synthetic_slots"
                covering = None
                best: tuple[int, int, int] | None = None  # dist, a, b
                for seg in segments:
                    try:
                        a = int(seg.get("startTime") or seg.get("start_time") or 0)
                        b = int(seg.get("endTime") or seg.get("end_time") or 0)
                    except (TypeError, ValueError):
                        continue
                    if b <= a:
                        continue
                    if a <= want < b:
                        covering = (a, b)
                        break
                    dist = want - b if want >= b else a - want
                    if dist < 0:
                        dist = -dist
                    if best is None or dist < best[0]:
                        best = (dist, a, b)
                snapped = False
                if covering is not None:
                    a, b = covering
                    end_time = min(want + int(max_seconds), b)
                    start_time = want
                    if end_time <= start_time:
                        end_time = min(a + int(max_seconds), b)
                        start_time = max(a, end_time - int(max_seconds))
                elif best is not None and best[0] <= 3600:
                    _dist, a, b = best
                    end_time = b
                    start_time = max(a, b - int(max_seconds))
                    snapped = True
                    skipped_empty.append(
                        {
                            "reason": "snapped_to_nearest_segment",
                            "requested": want,
                            "start_time": start_time,
                            "end_time": end_time,
                            "gap_s": _dist,
                        }
                    )
                else:
                    return {
                        "ok": False,
                        "error": (
                            "選定時間附近無錄影（已略過空等；"
                            "請改時間或點「載入時段」看有錄影範圍）"
                        ),
                        "timeline": timeline,
                        "requested_start": want,
                    }
                # Only probe synthetic IPC hours (NVR segments are authoritative).
                if synthetic and covering is not None:
                    await client.login()
                    assert client._stok
                    stream_port = int(
                        getattr(client, "stream_port", 8800) or 8800
                    )
                    probe = partial(
                        playback_has_mpegts,
                        client.host,
                        client.username,
                        client.password,
                        client._stok,
                        start_time,
                        min(int(end_time), int(start_time) + 60),
                        channel=channel,
                        stream_port=stream_port,
                        max_seconds=1.0,
                    )
                    try:
                        ok, nbytes, probe_meta = await hass.async_add_executor_job(
                            probe
                        )
                    except (TPLinkMediaStreamError, OSError, TimeoutError):
                        ok, nbytes, probe_meta = False, 0, {}
                    if not ok:
                        return {
                            "ok": False,
                            "error": (
                                "選定時段無畫面（可能僅移動偵測錄影）；"
                                "請選有錄影範圍內的時間"
                            ),
                            "timeline": timeline,
                            "requested_start": want,
                            "probe_bytes": nbytes,
                        }
                    if isinstance(probe_meta, dict):
                        if probe_meta.get("stream_port"):
                            preferred_port = int(probe_meta["stream_port"])
                        if "use_stok_path" in probe_meta:
                            preferred_use_stok = bool(probe_meta["use_stok_path"])
                if snapped:
                    pass

        try:
            st_i, et_i = int(start_time), int(end_time)
            if et_i - st_i > int(max_seconds) + 5:
                if from_timeline:
                    # 「最近錄影」播片段尾端（NVR 單段可達數小時）
                    start_time = et_i - int(max_seconds)
                    end_time = et_i
                else:
                    end_time = st_i + int(max_seconds)
        except (TypeError, ValueError):
            pass
        return {
            "ok": True,
            "start_time": start_time,
            "end_time": end_time,
            "segment_index": picked_idx,
            "skipped_empty": skipped_empty,
            "timeline_source": (timeline or {}).get("source"),
            "preferred_port": preferred_port,
            "preferred_use_stok": preferred_use_stok,
        }

    async def handle_stream_playback(call: ServiceCall) -> dict:
        """Stream recording via Streamd → localhost bridge → go2rtc WebRTC.

        Cameras without Streamd (e.g. 次卧) fall back to cloud relayservice
        download + ffmpeg file source when local probe/playback is empty.
        """
        client: TPLinkIPCClient | None = None
        try:
            client = await _client(call)
            channel = _channel_from_call(call)
            max_seconds = float(call.data.get(ATTR_MAX_SECONDS, 60))

            window = await _resolve_clip_window(client, call, channel)
            if not window.get("ok"):
                # Timeline may exist but Streamd probe empty — still try cloud.
                cloud_fallback = await _async_stream_via_cloud(
                    client, call, channel, max_seconds, window_error=window
                )
                if cloud_fallback is not None:
                    return cloud_fallback
                return window
            start_time = window["start_time"]
            end_time = window["end_time"]

            # Always refresh stok — go2rtc may connect minutes later
            await client.login()
            assert client._stok
        except (TPLinkIPCError, TimeoutError, OSError) as err:
            if client is not None:
                cloud_fallback = await _async_stream_via_cloud(
                    client,
                    call,
                    _channel_from_call(call),
                    float(call.data.get(ATTR_MAX_SECONDS, 60)),
                    window_error={"ok": False, "error": str(err)},
                )
                if cloud_fallback is not None:
                    return cloud_fallback
            return {"ok": False, "error": f"stream_playback setup failed: {err}"}

        bridge = await _async_ensure_playback_bridge(hass)
        stream_name = playback_stream_name(client.host, channel)
        entity_id = call.data.get("entity_id")
        if entity_id:
            st = hass.states.get(entity_id)
            if st is not None:
                named = st.attributes.get("webrtc_stream")
                if named:
                    stream_name = str(named)
                else:
                    ch_ip = st.attributes.get("channel_ip")
                    if ch_ip:
                        stream_name = playback_stream_name(str(ch_ip), 0)
        job = PlaybackJob(
            stream_name=stream_name,
            host=client.host,
            username=client.username,
            password=client.password,
            stok=client._stok,
            start_time=str(start_time),
            end_time=str(end_time),
            channel=channel,
            stream_port=int(getattr(client, "stream_port", 8800) or 8800),
            max_seconds=max(max_seconds + 15, 45),
            preferred_port=(
                int(window["preferred_port"])
                if window.get("preferred_port") is not None
                else None
            ),
            preferred_use_stok=window.get("preferred_use_stok"),
        )
        src = bridge.ffmpeg_src(stream_name, job.token)
        try:
            await hass.async_add_executor_job(patch_stream, stream_name, src)
        except Go2rtcError as err:
            return {
                "ok": False,
                "host": client.host,
                "error": f"go2rtc: {err}",
                "stream_name": stream_name,
                "go2rtc_src": src,
            }
        # Register only after go2rtc accepts the source (avoids token desync → 404).
        # Prefetch starts here so Streamd is already priming before WebRTC opens.
        await bridge.async_register(job)

        note = "已預熱串流；展開相機即可看畫面"
        skipped = window.get("skipped_empty") or []
        if skipped:
            labels = ", ".join(
                str(s.get("label") or s.get("start_time") or s.get("reason"))
                for s in skipped
                if s.get("reason") != "probe_ok"
            )
            if labels:
                note = f"已略過無錄影時段（{labels}）· {note}"
        payload = {
            "ok": True,
            "host": client.host,
            "channel": channel,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "source": "webrtc",
            "stream_name": stream_name,
            "webrtc_stream": stream_name,
            "bridge_url": bridge.mpegts_url(stream_name, job.token),
            "go2rtc_src": src,
            "max_seconds": max_seconds,
            "segment_index": window.get("segment_index"),
            "skipped_empty": skipped,
            "note": note,
        }
        for data in hass.data[DOMAIN].values():
            if not isinstance(data, dict):
                continue
            if data.get("client") is client:
                coordinator = data.get("coordinator")
                if coordinator:
                    coordinator.set_last_playback(channel, payload)
                break
        hass.bus.async_fire(f"{DOMAIN}_playback", payload)
        return payload

    async def _async_stream_via_cloud(
        client: TPLinkIPCClient,
        call: ServiceCall,
        channel: int,
        max_seconds: float,
        *,
        window_error: dict | None = None,
    ) -> dict | None:
        """Best-effort cloud clip → go2rtc ffmpeg file. None = not available."""
        device_id, device, relay = await _cloud_device_for_local(client)
        if not device_id or relay is None:
            return None
        start_time = call.data.get(ATTR_START_TIME)
        end_time = call.data.get(ATTR_END_TIME)
        if start_time is None or end_time is None:
            # Use timeline without Streamd probe
            date = call.data.get(ATTR_DATE)
            if not date:
                from homeassistant.util import dt as dt_util

                date = dt_util.now().strftime("%Y%m%d")
            try:
                timeline = await client.search_day_timeline(date, channel=channel)
            except (TPLinkIPCError, TimeoutError, OSError):
                timeline = {"ok": False}
            segments = list((timeline or {}).get("segments") or [])
            if not segments:
                return {
                    "ok": False,
                    "host": client.host,
                    "device_id": device_id,
                    "source": "cloud_relay",
                    "error": (window_error or {}).get("error")
                    or "no timeline for cloud fallback",
                    "hint": "請指定 start_time/end_time，或先載入當日時段",
                }
            idx = int(call.data.get(ATTR_SEGMENT_INDEX, 0))
            idx = max(0, min(idx, len(segments) - 1))
            seg = segments[idx]
            start_time = seg.get("startTime") or seg.get("start_time")
            end_time = seg.get("endTime") or seg.get("end_time")
        try:
            st_i, et_i = int(start_time), int(end_time)
            if et_i - st_i > int(max_seconds) + 5:
                start_time = et_i - int(max_seconds)
                end_time = et_i
        except (TypeError, ValueError):
            pass
        try:
            ts_data, meta = await relay.async_download_playback(
                device_id,
                start_time,
                end_time,
                channel=channel,
                max_bytes=12_000_000,
                max_seconds=max_seconds + 10,
                device=device,
            )
        except TPLinkCloudRelayError as err:
            return {
                "ok": False,
                "host": client.host,
                "device_id": device_id,
                "source": "cloud_relay",
                "error": str(err),
                "local_error": (window_error or {}).get("error"),
                "hint": (
                    "雲端媒體需 relayMaster 成功；"
                    "請呼叫 tplink_ipc.cloud_prepare_relay 查看 "
                    "getBizToken／cluster（已通）與 relayMaster（仍可能 -82419）"
                ),
            }
        www = Path(hass.config.path("www", "tplink_ipc"))
        www.mkdir(parents=True, exist_ok=True)
        stream_name = playback_stream_name(client.host, channel)
        entity_id = call.data.get("entity_id")
        if entity_id:
            st = hass.states.get(entity_id)
            if st is not None and st.attributes.get("webrtc_stream"):
                stream_name = str(st.attributes["webrtc_stream"])
        ts_path = www / f"{stream_name}_cloud.ts"
        ts_path.write_bytes(ts_data)
        # Absolute path for go2rtc on same host
        src = f"ffmpeg:{ts_path}#video=copy#audio=copy"
        try:
            await hass.async_add_executor_job(patch_stream, stream_name, src)
        except Go2rtcError as err:
            return {
                "ok": False,
                "host": client.host,
                "error": f"go2rtc: {err}",
                "stream_name": stream_name,
                "source": "cloud_relay",
            }
        payload = {
            "ok": True,
            "host": client.host,
            "channel": channel,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "source": "cloud_relay",
            "stream_name": stream_name,
            "webrtc_stream": stream_name,
            "go2rtc_src": src,
            "bytes": len(ts_data),
            "meta": meta,
            "note": "已用雲端片段餵給 go2rtc（非即時 Streamd）",
        }
        for data in hass.data[DOMAIN].values():
            if not isinstance(data, dict):
                continue
            if data.get("client") is client:
                coordinator = data.get("coordinator")
                if coordinator:
                    coordinator.set_last_playback(channel, payload)
                break
        hass.bus.async_fire(f"{DOMAIN}_playback", payload)
        return payload

    async def handle_play_recording(call: ServiceCall) -> dict:
        """Download a recording clip via media stream and remux to /local MP4."""
        client = await _client(call)
        channel = _channel_from_call(call)
        max_seconds = float(call.data.get(ATTR_MAX_SECONDS, 30))
        max_bytes = int(call.data.get(ATTR_MAX_BYTES, 12_000_000))

        window = await _resolve_clip_window(client, call, channel)
        if not window.get("ok"):
            return window
        start_time = window["start_time"]
        end_time = window["end_time"]

        source = "local"
        ts_data: bytes = b""
        meta: dict = {}
        local_err: str | None = None
        try:
            ts_data, meta = await client.download_playback_clip(
                start_time,
                end_time,
                channel=channel,
                max_bytes=max_bytes,
                max_seconds=max_seconds + 10,
            )
        except (TPLinkIPCError, TPLinkMediaStreamError) as err:
            local_err = str(err)
            ts_data, meta = b"", {"error": local_err}

        if len(ts_data) < 1024 and not getattr(client, "is_nvr", False):
            # Legacy -52407 was often client_id=-1; still try cloud if local empty
            device_id, device, relay = await _cloud_device_for_local(client)
            if device_id and relay is not None:
                try:
                    ts_data, meta = await relay.async_download_playback(
                        device_id,
                        start_time,
                        end_time,
                        channel=channel,
                        max_bytes=max_bytes,
                        max_seconds=max_seconds + 10,
                        device=device,
                    )
                    source = "cloud_relay"
                    local_err = None
                except TPLinkCloudRelayError as err:
                    return {
                        "ok": False,
                        "host": client.host,
                        "error": f"local: {local_err}; cloud_relay: {err}",
                        "device_id": device_id,
                        "source": "cloud_relay",
                        "hint": (
                            "次卧等無 Streamd 機種需雲端 relayMaster；"
                            "請呼叫 cloud_prepare_relay 查看進度"
                        ),
                    }
            elif local_err:
                return {"ok": False, "host": client.host, "error": local_err}

        if len(ts_data) < 1024:
            return {
                "ok": False,
                "host": client.host,
                "error": local_err or f"empty stream ({len(ts_data)} bytes)",
                "meta": meta,
                "source": source,
            }

        www = Path(hass.config.path("www", "tplink_ipc"))
        www.mkdir(parents=True, exist_ok=True)
        host_key = "".join(
            ch if ch.isalnum() else "_" for ch in str(client.host)
        )
        stem = f"{host_key}_ch{channel}_{start_time}_{end_time}"
        ts_path = www / f"{stem}.ts"
        mp4_path = www / f"{stem}.mp4"
        latest_mp4 = www / f"latest_{host_key}_ch{channel}.mp4"

        def _write_and_remux() -> dict:
            ts_path.write_bytes(ts_data)
            # Prefer HA image ffmpeg (HEVC-capable)
            ffmpeg = "ffmpeg"
            for candidate in ("/usr/bin/ffmpeg", "ffmpeg"):
                if candidate == "ffmpeg" or os.path.isfile(candidate):
                    ffmpeg = candidate
                    break
            cmd = [
                ffmpeg,
                "-y",
                "-analyzeduration",
                "10M",
                "-probesize",
                "10M",
                "-i",
                str(ts_path),
                "-map",
                "0:v:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-movflags",
                "+faststart",
                str(mp4_path),
            ]
            import subprocess

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            ok = proc.returncode == 0 and mp4_path.is_file() and mp4_path.stat().st_size > 0
            if not ok:
                # Fallback: stream copy (HEVC) — plays on Safari / some Android
                cmd2 = [
                    ffmpeg,
                    "-y",
                    "-analyzeduration",
                    "10M",
                    "-probesize",
                    "10M",
                    "-i",
                    str(ts_path),
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-an",
                    "-movflags",
                    "+faststart",
                    str(mp4_path),
                ]
                proc = subprocess.run(cmd2, capture_output=True, text=True, timeout=120)
                ok = proc.returncode == 0 and mp4_path.is_file() and mp4_path.stat().st_size > 0
            if ok:
                latest_mp4.write_bytes(mp4_path.read_bytes())
            return {
                "remux_ok": ok,
                "ffmpeg_rc": proc.returncode,
                "ffmpeg_err": (proc.stderr or "")[-400:],
                "ts_bytes": len(ts_data),
                "mp4_bytes": mp4_path.stat().st_size if mp4_path.is_file() else 0,
            }

        remux = await hass.async_add_executor_job(_write_and_remux)
        local_url = f"/local/tplink_ipc/{mp4_path.name}" if remux.get("remux_ok") else None
        payload = {
            "ok": bool(remux.get("remux_ok")),
            "host": client.host,
            "channel": channel,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "source": source,
            "local_url": local_url,
            "latest_url": (
                f"/local/tplink_ipc/latest_{host_key}_ch{channel}.mp4"
                if remux.get("remux_ok")
                else None
            ),
            "ts_path": str(ts_path),
            "mp4_path": str(mp4_path) if remux.get("remux_ok") else None,
            **remux,
            "meta": meta,
        }
        if not payload["ok"]:
            # Still expose TS for debugging / alternate players
            payload["ts_url"] = f"/local/tplink_ipc/{ts_path.name}"
            payload["error"] = remux.get("ffmpeg_err") or "remux failed"

        for data in hass.data[DOMAIN].values():
            if data.get("client") is client:
                coordinator = data.get("coordinator")
                if coordinator:
                    coordinator.set_last_playback(channel, payload)
                break
        hass.bus.async_fire(f"{DOMAIN}_playback", payload)
        return payload

    entity_opt = {vol.Optional("entity_id"): cv.entity_id}

    if register_local:
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_MOVE,
            handle_ptz_move,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Required(ATTR_X): vol.Any(int, float, str),
                    vol.Required(ATTR_Y): vol.Any(int, float, str),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_STEP,
            handle_ptz_step,
            schema=vol.Schema({**entity_opt, vol.Required(ATTR_DIRECTION): cv.string}),
        )
        hass.services.async_register(
            DOMAIN, SERVICE_PTZ_STOP, handle_ptz_stop, schema=vol.Schema(entity_opt)
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_CONTINUOUS,
            handle_ptz_continuous,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Optional(ATTR_PAN, default=0): vol.Coerce(float),
                    vol.Optional(ATTR_TILT, default=0): vol.Coerce(float),
                    vol.Optional(ATTR_ZOOM, default=0): vol.Coerce(float),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_ZOOM,
            handle_ptz_zoom,
            schema=vol.Schema(
                {**entity_opt, vol.Required(ATTR_ZOOM): vol.Coerce(float)}
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_CALIBRATE,
            handle_ptz_calibrate,
            schema=vol.Schema(entity_opt),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_PRESET,
            handle_ptz_preset,
            schema=vol.Schema(
                {**entity_opt, vol.Required(ATTR_PRESET_ID): vol.Any(int, str)}
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_SET_PRESET,
            handle_ptz_set_preset,
            schema=vol.Schema(
                {**entity_opt, vol.Required(ATTR_PRESET_NAME): cv.string}
            ),
        )
        hass.services.async_register(
            DOMAIN, SERVICE_REBOOT, handle_reboot, schema=vol.Schema(entity_opt)
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_FORMAT_DISK,
            handle_format_disk,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Optional(ATTR_DISK_NAME, default="1"): cv.string,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_FORMAT_SD,
            handle_format_sd,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Optional(ATTR_SD_NAME, default="1"): cv.string,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_FORMAT_STATUS,
            handle_get_format_status,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Optional(ATTR_DISK_NAME, default="1"): cv.string,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_TIMING_REBOOT,
            handle_set_timing_reboot,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Optional(ATTR_ENABLED): vol.Any(bool, str),
                    vol.Optional(ATTR_DAY): vol.Coerce(int),
                    vol.Optional(ATTR_TIME): cv.string,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_DOORBELL_WAKEUP,
            handle_doorbell_wakeup,
            schema=vol.Schema(entity_opt),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_TRIGGER_ALARM,
            handle_trigger_alarm,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Optional(ATTR_SECONDS, default=5): vol.Coerce(float),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_RAW,
            handle_raw,
            schema=vol.Schema({**entity_opt, vol.Required("payload"): dict}),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH_DAYS,
            handle_search_days,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Required(ATTR_START_DATE): cv.string,
                    vol.Required(ATTR_END_DATE): cv.string,
                    vol.Optional(ATTR_CHANNEL, default=0): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH_VIDEO,
            handle_search_video,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Required(ATTR_DATE): cv.string,
                    vol.Optional(ATTR_CHANNEL, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_START_INDEX, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_END_INDEX, default=99): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH_EVENTS,
            handle_search_events,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Required(ATTR_START_TIME): vol.Any(cv.string, int),
                    vol.Required(ATTR_END_TIME): vol.Any(cv.string, int),
                    vol.Optional(ATTR_CHANNEL, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_CLIENT_ID, default=-1): vol.Coerce(int),
                    vol.Optional(ATTR_START_INDEX, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_MAX_NUM, default=100): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH_TIMELINE,
            handle_search_timeline,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Required(ATTR_DATE): cv.string,
                    vol.Optional(ATTR_CHANNEL): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_CALENDARS,
            handle_refresh_calendars,
            schema=vol.Schema(entity_opt),
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PLAY_RECORDING,
            handle_play_recording,
            schema=vol.Schema(
                {
                    **entity_opt,
                    vol.Optional(ATTR_DATE): cv.string,
                    vol.Optional(ATTR_START_TIME): vol.Any(cv.string, int),
                    vol.Optional(ATTR_END_TIME): vol.Any(cv.string, int),
                    vol.Optional(ATTR_CHANNEL): vol.Coerce(int),
                    vol.Optional(ATTR_SEGMENT_INDEX, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_MAX_SECONDS, default=30): vol.Coerce(float),
                    vol.Optional(ATTR_MAX_BYTES, default=12_000_000): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
    # stream_playback: always re-register (even on config-entry reload)
    hass.services.async_register(
        DOMAIN,
        SERVICE_STREAM_PLAYBACK,
        handle_stream_playback,
        schema=vol.Schema(
            {
                **entity_opt,
                vol.Optional(ATTR_DATE): cv.string,
                vol.Optional(ATTR_START_TIME): vol.Any(cv.string, int),
                vol.Optional(ATTR_END_TIME): vol.Any(cv.string, int),
                vol.Optional(ATTR_CHANNEL): vol.Coerce(int),
                vol.Optional(ATTR_SEGMENT_INDEX, default=0): vol.Coerce(int),
                vol.Optional(ATTR_MAX_SECONDS, default=60): vol.Coerce(float),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    if register_cloud:
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLOUD_DEVICE_LIST,
            handle_cloud_device_list,
            schema=vol.Schema({}),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLOUD_PASSTHROUGH,
            handle_cloud_passthrough,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DEVICE_ID): cv.string,
                    vol.Required(ATTR_PAYLOAD): dict,
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )

    if register_cloud or register_cloud_ux:
        _device_schema = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
        for svc, handler in (
            (SERVICE_CLOUD_REBOOT, handle_cloud_reboot),
            (SERVICE_CLOUD_GET_DISK, handle_cloud_get_disk),
            (SERVICE_CLOUD_GET_RECORD_PLAN, handle_cloud_get_record_plan),
            (SERVICE_CLOUD_GET_DEVICE_INFO, handle_cloud_get_device_info),
        ):
            if not hass.services.has_service(DOMAIN, svc):
                hass.services.async_register(
                    DOMAIN,
                    svc,
                    handler,
                    schema=_device_schema,
                    supports_response=SupportsResponse.ONLY,
                )

    # Cloud media services (bizToken / cluster / relayservice)
    if not hass.services.has_service(DOMAIN, SERVICE_CLOUD_GET_BIZ_TOKEN):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLOUD_GET_BIZ_TOKEN,
            handle_cloud_get_biz_token,
            schema=vol.Schema(
                {vol.Optional(ATTR_BIZ_SERVICE_ID): cv.string}
            ),
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CLOUD_GET_RELAY_CLUSTER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLOUD_GET_RELAY_CLUSTER,
            handle_cloud_get_relay_cluster,
            schema=vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string}),
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CLOUD_PREPARE_RELAY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLOUD_PREPARE_RELAY,
            handle_cloud_prepare_relay,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DEVICE_ID): cv.string,
                    vol.Optional(ATTR_STREAM_TYPE, default="sdvod"): cv.string,
                    vol.Optional(ATTR_CHANNEL, default=0): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CLOUD_PLAY_RECORDING):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLOUD_PLAY_RECORDING,
            handle_cloud_play_recording,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DEVICE_ID): cv.string,
                    vol.Required(ATTR_START_TIME): vol.Any(int, float, cv.string),
                    vol.Required(ATTR_END_TIME): vol.Any(int, float, cv.string),
                    vol.Optional(ATTR_CHANNEL, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_MAX_SECONDS, default=30): vol.Coerce(float),
                    vol.Optional(ATTR_MAX_BYTES, default=12_000_000): vol.Coerce(int),
                    vol.Optional(ATTR_STREAM_TYPE, default="sdvod"): cv.string,
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )

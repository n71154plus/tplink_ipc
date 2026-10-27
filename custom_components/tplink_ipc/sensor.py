"""Sensor entities."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TPLinkIPCClient
from .cloud import TPLinkCloudClient
from .cloud_coordinator import TPLinkCloudCoordinator
from .cloud_entity import TPLinkCloudDeviceEntity, TPLinkCloudEntity
from .const import (
    CONF_CONNECTION_TYPE,
    CONN_CLOUD,
    DOMAIN,
    GO2RTC_LIVE_BY_HOST,
    GO2RTC_PLAYBACK_BY_HOST,
    playback_stream_name,
)
from .coordinator import TPLinkIPCCoordinator
from .entity import TPLinkIPCEntity
from .features import dig

# APK / web UI aligned storage status labels (zh-Hant defaults; entity state stays raw)
_STORAGE_STATUS_ZH = {
    "normal": "正常",
    "unformatted": "未格式化",
    "formatting": "格式化中",
    "full": "已滿",
    "offline": "離線",
    "error": "錯誤",
    "abnormal": "異常",
}


class TPLinkIPCIpSensor(TPLinkIPCEntity, SensorEntity):
    _attr_translation_key = "ip"
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "ip")

    @property
    def native_value(self) -> str | None:
        return dig(self.coordinator.data or {}, ("network", "wan_status", "ipaddr"))


class TPLinkIPCDiskStatusSensor(TPLinkIPCEntity, SensorEntity):
    _attr_translation_key = "disk_status"
    _attr_icon = "mdi:harddisk"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "disk_status")

    def _disk_row(self) -> dict | None:
        hd = dig(self.coordinator.data or {}, ("harddisk_manage", "hd_info"))
        if not isinstance(hd, list) or not hd:
            return None
        first = hd[0]
        if isinstance(first, dict):
            for v in first.values():
                if isinstance(v, dict):
                    return v
        return None

    @property
    def native_value(self) -> str | None:
        row = self._disk_row()
        if not row:
            return None
        raw = row.get("status")
        if raw is None:
            return None
        key = str(raw).lower()
        return _STORAGE_STATUS_ZH.get(key, str(raw))

    @property
    def extra_state_attributes(self) -> dict | None:
        row = self._disk_row()
        if not row:
            return None
        return {
            "status_raw": row.get("status"),
            "disk_name": row.get("disk_name"),
            "total_space": row.get("total_space"),
            "free_space": row.get("free_space"),
            "type": row.get("type"),
            "detect_status": row.get("detect_status"),
            "record_duration": row.get("record_duration"),
            "record_free_duration": row.get("record_free_duration"),
            "write_protect": row.get("write_protect"),
            "loop_record_status": row.get("loop_record_status"),
            "hint": (
                "未格式化時可用服務 tplink_ipc.format_disk（會清空硬碟）"
                if str(row.get("status") or "").lower() == "unformatted"
                else None
            ),
        }


class TPLinkIPCBatterySensor(TPLinkIPCEntity, SensorEntity):
    """Local battery doorbell charge level (APK battery.setting)."""

    _attr_translation_key = "battery"
    _attr_icon = "mdi:battery"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "battery")

    @property
    def available(self) -> bool:
        bat = dig(self.coordinator.data or {}, ("battery", "setting"))
        return super().available and isinstance(bat, dict)

    @property
    def native_value(self) -> float | None:
        bat = dig(self.coordinator.data or {}, ("battery", "setting")) or {}
        for key in ("percent", "battery_percent", "soc", "level", "battery"):
            if key in bat and bat[key] is not None:
                try:
                    return float(bat[key])
                except (TypeError, ValueError):
                    continue
        return None

    @property
    def extra_state_attributes(self) -> dict | None:
        return dig(self.coordinator.data or {}, ("battery", "setting"))


class TPLinkIPCTimingRebootSensor(TPLinkIPCEntity, SensorEntity):
    """Scheduled reboot summary (APK timing_reboot)."""

    _attr_translation_key = "timing_reboot"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "timing_reboot_info")

    @property
    def available(self) -> bool:
        return super().available and dig(
            self.coordinator.data or {}, ("timing_reboot", "reboot")
        ) is not None

    @property
    def native_value(self) -> str | None:
        reb = dig(self.coordinator.data or {}, ("timing_reboot", "reboot")) or {}
        if not reb:
            return None
        en = str(reb.get("enabled") or "off")
        return f"{en} @ {reb.get('time') or '?'}"

    @property
    def extra_state_attributes(self) -> dict | None:
        return dig(self.coordinator.data or {}, ("timing_reboot", "reboot"))


class TPLinkIPCRecordPlanSensor(TPLinkIPCEntity, SensorEntity):
    """Record plan schedule (APK record_plan week strings)."""

    _attr_translation_key = "record_plan"
    _attr_icon = "mdi:calendar-range"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "record_plan_info")

    @property
    def available(self) -> bool:
        return super().available and dig(
            self.coordinator.data or {}, ("record_plan", "chn1_channel")
        ) is not None

    @property
    def native_value(self) -> str | None:
        ch = dig(self.coordinator.data or {}, ("record_plan", "chn1_channel")) or {}
        return str(ch.get("enabled") or "unknown")

    @property
    def extra_state_attributes(self) -> dict | None:
        from urllib.parse import unquote

        ch = dig(self.coordinator.data or {}, ("record_plan", "chn1_channel"))
        if not isinstance(ch, dict):
            return None
        out = {"enabled": ch.get("enabled")}
        for day in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ):
            if day in ch:
                out[day] = unquote(str(ch[day]))
        return out


class TPLinkIPCDiskPercentSensor(TPLinkIPCEntity, SensorEntity):
    _attr_translation_key = "disk_percent"
    _attr_icon = "mdi:database"
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "disk_percent")

    @property
    def native_value(self) -> float | None:
        hd = dig(self.coordinator.data or {}, ("harddisk_manage", "hd_info"))
        if not isinstance(hd, list) or not hd:
            return None
        first = hd[0]
        if isinstance(first, dict):
            for v in first.values():
                if isinstance(v, dict) and v.get("percent") is not None:
                    try:
                        return float(v["percent"])
                    except (TypeError, ValueError):
                        return None
        return None


class TPLinkIPCVideoMainSensor(TPLinkIPCEntity, SensorEntity):
    _attr_translation_key = "video_main"
    _attr_icon = "mdi:video"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "video_main")

    @property
    def native_value(self) -> str | None:
        main = dig(self.coordinator.data or {}, ("video", "main")) or {}
        if not main:
            return None
        return f"{main.get('encode_type')} {main.get('resolution')}"

    @property
    def extra_state_attributes(self) -> dict | None:
        return dig(self.coordinator.data or {}, ("video", "main"))


class TPLinkIPCPresetCountSensor(TPLinkIPCEntity, SensorEntity):
    _attr_translation_key = "preset_count"
    _attr_icon = "mdi:map-marker-multiple"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "preset_count")

    @property
    def native_value(self) -> int | None:
        ids = dig(self.coordinator.data or {}, ("preset", "preset", "id"))
        if isinstance(ids, list):
            return len(ids)
        return None

    @property
    def extra_state_attributes(self) -> dict | None:
        preset = dig(self.coordinator.data or {}, ("preset", "preset"))
        if not isinstance(preset, dict):
            return None
        ids = preset.get("id") or []
        names = preset.get("name") or []
        return {
            "presets": [
                {"id": i, "name": names[idx] if idx < len(names) else None}
                for idx, i in enumerate(ids)
            ]
        }


class TPLinkIPCRecordingCalendarSensor(TPLinkIPCEntity, SensorEntity):
    """APK calendar: count of days with local recordings."""

    _attr_translation_key = "recording_calendar"
    _attr_icon = "mdi:calendar-month"

    def __init__(
        self,
        coordinator,
        client,
        entry,
        playback_channel: int = 0,
        channel_label: str | None = None,
        channel_ip: str | None = None,
    ) -> None:
        suffix = (
            ""
            if playback_channel == 0 and not client.is_nvr
            else f"_ch{playback_channel}"
        )
        super().__init__(coordinator, client, entry, f"recording_calendar{suffix}")
        self._playback_channel = playback_channel
        self._channel_label = channel_label
        self._channel_ip = channel_ip
        if channel_label:
            # Device channel alias is already localized (e.g. 露台)
            self._attr_name = f"錄影日曆 · {channel_label}"
            self._attr_translation_key = None

    def _resolve_camera_entity_id(self) -> str | None:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        mac = self._entry.unique_id
        if self._client.is_nvr:
            hw = self._playback_channel + 1
            candidates = (f"{mac}_ch{hw}_camera", f"{mac}_camera")
        else:
            candidates = (f"{mac}_camera",)
        for want in candidates:
            for ent in registry.entities.values():
                if (
                    ent.unique_id == want
                    and ent.config_entry_id == self._entry.entry_id
                ):
                    return ent.entity_id
        return None

    def _go2rtc_stream(self) -> str | None:
        host = (self._channel_ip or self._client.host or "").strip()
        return GO2RTC_LIVE_BY_HOST.get(host)

    def _go2rtc_playback_stream(self) -> str:
        host = (self._channel_ip or self._client.host or "").strip()
        if host in GO2RTC_PLAYBACK_BY_HOST and self._playback_channel == 0:
            return GO2RTC_PLAYBACK_BY_HOST[host]
        return playback_stream_name(host or self._client.host, self._playback_channel)

    @property
    def native_value(self) -> int:
        days = (getattr(self.coordinator, "recording_days_by_channel", {}) or {}).get(
            self._playback_channel
        )
        if days is None and self._playback_channel == 0:
            days = getattr(self.coordinator, "recording_days", None) or []
        return len(days or [])

    @property
    def extra_state_attributes(self) -> dict:
        by_ch = getattr(self.coordinator, "recording_days_by_channel", {}) or {}
        days = list(by_ch.get(self._playback_channel) or [])
        if not days and self._playback_channel == 0:
            days = list(getattr(self.coordinator, "recording_days", None) or [])
        err = getattr(self.coordinator, "recording_days_error", None)
        by_tl = getattr(self.coordinator, "last_timeline_by_channel", None) or {}
        by_pb = getattr(self.coordinator, "last_playback_by_channel", None) or {}
        timeline = dict(by_tl.get(self._playback_channel) or {})
        playback = dict(by_pb.get(self._playback_channel) or {})
        # Legacy single-slot fallback (pre 0.9.8)
        if not timeline:
            legacy_tl = getattr(self.coordinator, "last_timeline", None) or {}
            if legacy_tl.get("channel") == self._playback_channel:
                timeline = dict(legacy_tl)
        if not playback:
            legacy_pb = getattr(self.coordinator, "last_playback", None) or {}
            if legacy_pb.get("channel") == self._playback_channel:
                playback = dict(legacy_pb)
        cam = self._resolve_camera_entity_id()
        host_key = "".join(
            ch if ch.isalnum() else "_" for ch in str(self._client.host)
        )
        latest = (
            f"/local/tplink_ipc/latest_{host_key}_ch{self._playback_channel}.mp4"
        )
        spec = self._client.module_spec or {}
        storage = self._client.storage_info()
        # NVRHttpStream (:8000) + IPC Streamd (:8800, client_id=2) both work.
        # Some IPC (e.g. bedroom) have no Streamd port → need P2P/cloud later.
        pb_modes = spec.get("playback") or []
        supports_local = bool(self._client.is_nvr) or (
            isinstance(pb_modes, list) and "local" in pb_modes
        )
        clip_playable = supports_local
        last_src = (playback or {}).get("source")
        if last_src == "webrtc":
            clip_playable = True
            clip_note = "WebRTC 串流回放（go2rtc）"
        elif last_src == "cloud_relay":
            clip_playable = True
            clip_note = "上次片段來自雲端 relay"
        elif self._client.is_nvr:
            clip_note = "NVR 本機回放可用（WebRTC）"
        elif supports_local:
            clip_note = "獨立 IPC：Streamd → WebRTC"
        else:
            clip_note = (
                "獨立 IPC：日曆可用；無本機 Streamd 時需 P2P／雲端"
            )
        return {
            "days": days,
            "latest": days[-1] if days else None,
            "playback_channel": self._playback_channel,
            "channel_label": self._channel_label,
            "channel_ip": self._channel_ip,
            "playback_version": self._client.playback_version,
            "storage_api_version": spec.get("storage_api_version"),
            "streaming_support_versions": spec.get("streaming_support_versions"),
            "is_nvr": self._client.is_nvr,
            "clip_playable": clip_playable,
            "clip_note": clip_note,
            "source": last_src or ("local_nvr" if self._client.is_nvr else "local_ipc"),
            "camera_entity_id": cam,
            "go2rtc_stream": self._go2rtc_stream(),
            "webrtc_stream": self._go2rtc_playback_stream(),
            "latest_url": latest,
            "channels": self._client.list_channels() if self._client.is_nvr else [],
            "error": err,
            "storage": storage,
            "last_timeline": timeline,
            "last_playback": playback,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get(CONF_CONNECTION_TYPE) == CONN_CLOUD:
        await _async_setup_cloud_sensors(hass, entry, data, async_add_entities)
        return

    coordinator: TPLinkIPCCoordinator = data["coordinator"]
    client: TPLinkIPCClient = data["client"]

    entities: list[SensorEntity] = [
        TPLinkIPCIpSensor(coordinator, client, entry),
        TPLinkIPCDiskStatusSensor(coordinator, client, entry),
        TPLinkIPCDiskPercentSensor(coordinator, client, entry),
        TPLinkIPCVideoMainSensor(coordinator, client, entry),
        TPLinkIPCPresetCountSensor(coordinator, client, entry),
    ]
    state = coordinator.data or {}
    if dig(state, ("battery", "setting")) is not None:
        entities.append(TPLinkIPCBatterySensor(coordinator, client, entry))
    if dig(state, ("timing_reboot", "reboot")) is not None:
        entities.append(TPLinkIPCTimingRebootSensor(coordinator, client, entry))
    if dig(state, ("record_plan", "chn1_channel")) is not None:
        entities.append(TPLinkIPCRecordPlanSensor(coordinator, client, entry))

    if client.is_nvr and client.list_channels():
        for ch in client.list_channels():
            entities.append(
                TPLinkIPCRecordingCalendarSensor(
                    coordinator,
                    client,
                    entry,
                    playback_channel=ch["playback_channel"],
                    channel_label=ch.get("name") or f"CH{ch['id']}",
                    channel_ip=ch.get("ip"),
                )
            )
    else:
        entities.append(
            TPLinkIPCRecordingCalendarSensor(coordinator, client, entry)
        )

    async_add_entities(entities)


async def _async_setup_cloud_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    data: dict,
    async_add_entities: AddEntitiesCallback,
) -> None:
    from .cloud_helpers import is_doorbell, is_ipc_family, is_router_family

    coordinator: TPLinkCloudCoordinator = data["cloud_coordinator"]
    client: TPLinkCloudClient = data["cloud_client"]
    known: set[str] = set()

    def _entities_for(device_ids: set[str]) -> list[SensorEntity]:
        out: list[SensorEntity] = []
        devices = (coordinator.data or {}).get("devices") or {}
        for did in device_ids:
            dev = devices.get(did) or {}
            out.append(
                TPLinkCloudDeviceOnlineSensor(coordinator, client, entry, did)
            )
            if is_router_family(dev):
                out.extend(
                    [
                        TPLinkCloudWanIpSensor(coordinator, client, entry, did),
                        TPLinkCloudWanLinkSensor(
                            coordinator, client, entry, did
                        ),
                        TPLinkCloudWanSpeedSensor(
                            coordinator, client, entry, did, "down"
                        ),
                        TPLinkCloudWanSpeedSensor(
                            coordinator, client, entry, did, "up"
                        ),
                        TPLinkCloudClientCountSensor(
                            coordinator, client, entry, did
                        ),
                        TPLinkCloudUptimeSensor(coordinator, client, entry, did),
                    ]
                )
            if is_ipc_family(dev):
                out.extend(
                    [
                        TPLinkCloudDiskStatusSensor(
                            coordinator, client, entry, did
                        ),
                        TPLinkCloudDiskPercentSensor(
                            coordinator, client, entry, did
                        ),
                        TPLinkCloudIpSensor(coordinator, client, entry, did),
                        TPLinkCloudVideoMainSensor(
                            coordinator, client, entry, did
                        ),
                    ]
                )
            if is_doorbell(dev):
                out.append(
                    TPLinkCloudBatterySensor(coordinator, client, entry, did)
                )
        return out

    account = TPLinkCloudAccountSensor(coordinator, client, entry)
    devices = (coordinator.data or {}).get("devices") or {}
    known.update(devices)
    async_add_entities([account, *_entities_for(known)])

    @callback
    def _on_update() -> None:
        current = set((coordinator.data or {}).get("devices") or {})
        new_ids = current - known
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(_entities_for(new_ids))

    entry.async_on_unload(coordinator.async_add_listener(_on_update))


class TPLinkCloudAccountSensor(TPLinkCloudEntity, SensorEntity):
    """Cloud account summary (bound device count)."""

    _attr_translation_key = "cloud_device_count"
    _attr_icon = "mdi:cloud"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "cloud_device_count")

    @property
    def native_value(self) -> int:
        return int((self.coordinator.data or {}).get("count") or 0)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        devices = data.get("devices") or {}
        return {
            "username": data.get("username") or self._client.username,
            "online_count": data.get("online_count"),
            "ipc_count": data.get("ipc_count"),
            "router_count": data.get("router_count"),
            "devices": [
                {
                    "alias": d.get("alias") or d.get("deviceName"),
                    "model": d.get("deviceModel"),
                    "type": d.get("deviceType"),
                    "online": d.get("online"),
                    "device_id": did,
                    "mac": d.get("mac_norm") or d.get("deviceMac"),
                    "matched_local_entry": d.get("matched_local_entry"),
                }
                for did, d in devices.items()
            ],
        }


class TPLinkCloudDeviceOnlineSensor(TPLinkCloudDeviceEntity, SensorEntity):
    """Per-device online status from getDeviceList."""

    _attr_translation_key = "cloud_device_online"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["online", "offline"]

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        from .cloud_helpers import icon_for_type

        super().__init__(coordinator, client, entry, device_id, "online")
        self._attr_icon = icon_for_type(self.device)

    @property
    def native_value(self) -> str:
        return "online" if self.device.get("online") else "offline"

    @property
    def extra_state_attributes(self) -> dict:
        d = self.device
        err = self.device_state.get("_error")
        return {
            "alias": d.get("alias") or d.get("deviceName"),
            "model": d.get("deviceModel") or d.get("model"),
            "mac": d.get("mac_norm") or d.get("deviceMac"),
            "device_id": self._device_id,
            "status_raw": d.get("status"),
            "device_type": d.get("deviceType") or d.get("type"),
            "ipc_family": d.get("ipc_family"),
            "matched_local_entry": d.get("matched_local_entry"),
            "fw_ver": d.get("fwVer") or d.get("fw_ver"),
            "region": d.get("region"),
            "state_error": err,
        }


class TPLinkCloudDiskStatusSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "disk_status"
    _attr_icon = "mdi:harddisk"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "disk_status")

    @property
    def available(self) -> bool:
        from .cloud_helpers import first_hd_info

        return (
            super().available
            and bool(self.device.get("online"))
            and first_hd_info(self.device_state) is not None
        )

    @property
    def native_value(self) -> str | None:
        from .cloud_helpers import first_hd_info

        info = first_hd_info(self.device_state)
        if not info:
            return None
        raw = info.get("status") or info.get("detect_status")
        if raw is None:
            return None
        key = str(raw).lower()
        return _STORAGE_STATUS_ZH.get(key, str(raw))

    @property
    def extra_state_attributes(self) -> dict | None:
        from .cloud_helpers import first_hd_info

        info = first_hd_info(self.device_state)
        if not info:
            return None
        return {
            "status_raw": info.get("status"),
            "total_space": info.get("total_space") or info.get("video_total_space"),
            "free_space": info.get("free_space") or info.get("video_free_space"),
            "type": info.get("type"),
            "detect_status": info.get("detect_status"),
            "loop_record_status": info.get("loop_record_status"),
            "record_duration": info.get("record_duration"),
            "record_free_duration": info.get("record_free_duration"),
        }


class TPLinkCloudDiskPercentSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "disk_percent"
    _attr_icon = "mdi:database"
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "disk_percent")

    @property
    def available(self) -> bool:
        from .cloud_helpers import first_hd_info

        info = first_hd_info(self.device_state)
        return (
            super().available
            and bool(self.device.get("online"))
            and info is not None
            and info.get("percent") is not None
        )

    @property
    def native_value(self) -> float | None:
        from .cloud_helpers import first_hd_info

        info = first_hd_info(self.device_state)
        if not info or info.get("percent") is None:
            return None
        try:
            return float(info["percent"])
        except (TypeError, ValueError):
            return None


class TPLinkCloudIpSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "ip"
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "ip")

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self.device.get("online"))
            and self._state_path(("network", "wan_status", "ipaddr")) is not None
        )

    @property
    def native_value(self) -> str | None:
        return self._state_path(("network", "wan_status", "ipaddr"))


class TPLinkCloudVideoMainSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "video_main"
    _attr_icon = "mdi:video"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "video_main")

    @property
    def available(self) -> bool:
        main = self._state_path(("video", "main"))
        return (
            super().available
            and bool(self.device.get("online"))
            and isinstance(main, dict)
            and bool(main)
            and any(
                v not in (None, "", [], {})
                for k, v in main.items()
                if not str(k).startswith(".")
            )
        )

    @property
    def native_value(self) -> str | None:
        main = self._state_path(("video", "main")) or {}
        if not isinstance(main, dict):
            return None
        res = main.get("resolution")
        enc = main.get("encode_type") or main.get("encoding")
        br = main.get("bitrate")
        parts = [p for p in (res, enc, f"{br}kbps" if br else None) if p]
        return " · ".join(str(p) for p in parts) if parts else None

    @property
    def extra_state_attributes(self) -> dict | None:
        main = self._state_path(("video", "main"))
        return main if isinstance(main, dict) else None


class TPLinkCloudWanIpSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "cloud_wan_ip"
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "wan_ip")

    @property
    def available(self) -> bool:
        from .cloud_helpers import wan_status

        return (
            super().available
            and bool(self.device.get("online"))
            and bool(wan_status(self.device_state).get("ipaddr"))
        )

    @property
    def native_value(self) -> str | None:
        from .cloud_helpers import wan_status

        return wan_status(self.device_state).get("ipaddr")

    @property
    def extra_state_attributes(self) -> dict:
        from .cloud_helpers import wan_status

        wan = wan_status(self.device_state)
        lan = self._state_path(("network", "lan_status"))
        return {
            "proto": wan.get("proto"),
            "netmask": wan.get("netmask"),
            "pri_dns": wan.get("pri_dns"),
            "snd_dns": wan.get("snd_dns"),
            "gateway": wan.get("gateway"),
            "link_status": wan.get("link_status"),
            "phy_status": wan.get("phy_status"),
            "lan_status": lan,
            "iface_mac": self._state_path(("network", "iface_mac")),
        }


class TPLinkCloudWanLinkSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "cloud_wan_link"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["up", "down"]
    _attr_icon = "mdi:wan"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "wan_link")

    @property
    def available(self) -> bool:
        from .cloud_helpers import wan_status

        return (
            super().available
            and bool(self.device.get("online"))
            and wan_status(self.device_state).get("link_status") is not None
        )

    @property
    def native_value(self) -> str | None:
        from .cloud_helpers import wan_status

        link = wan_status(self.device_state).get("link_status")
        if link is None:
            return None
        try:
            return "up" if int(link) == 1 else "down"
        except (TypeError, ValueError):
            return "up" if str(link) in ("1", "up", "true") else "down"


class TPLinkCloudWanSpeedSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_icon = "mdi:speedometer"
    _attr_native_unit_of_measurement = "KB/s"

    def __init__(
        self, coordinator, client, entry, device_id: str, direction: str
    ) -> None:
        key = f"wan_{direction}_speed"
        super().__init__(coordinator, client, entry, device_id, key)
        self._direction = direction
        self._attr_translation_key = (
            "cloud_wan_down_speed" if direction == "down" else "cloud_wan_up_speed"
        )

    @property
    def available(self) -> bool:
        from .cloud_helpers import wan_status

        field = "down_speed" if self._direction == "down" else "up_speed"
        return (
            super().available
            and bool(self.device.get("online"))
            and wan_status(self.device_state).get(field) is not None
        )

    @property
    def native_value(self) -> float | None:
        from .cloud_helpers import wan_status

        field = "down_speed" if self._direction == "down" else "up_speed"
        try:
            return float(wan_status(self.device_state).get(field))
        except (TypeError, ValueError):
            return None


class TPLinkCloudClientCountSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "cloud_client_count"
    _attr_icon = "mdi:lan"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "client_count")

    @property
    def available(self) -> bool:
        return super().available and bool(self.device.get("online"))

    @property
    def native_value(self) -> int:
        from .cloud_helpers import iter_hosts

        return len(iter_hosts(self.device_state))

    @property
    def extra_state_attributes(self) -> dict:
        from .cloud_helpers import iter_hosts

        hosts = iter_hosts(self.device_state)
        clients = []
        for h in hosts[:40]:
            clients.append(
                {
                    "hostname": h.get("hostname") or h.get("name"),
                    "ip": h.get("ip"),
                    "mac": h.get("mac"),
                    "type": h.get("type"),
                    "wifi_mode": h.get("wifi_mode"),
                    "ssid": h.get("ssid"),
                    "up_speed": h.get("up_speed"),
                    "down_speed": h.get("down_speed"),
                    "blocked": h.get("blocked"),
                }
            )
        return {"clients": clients, "truncated": len(hosts) > 40}


class TPLinkCloudUptimeSensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "cloud_wan_uptime"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "wan_uptime")

    @property
    def available(self) -> bool:
        from .cloud_helpers import wan_status

        return (
            super().available
            and bool(self.device.get("online"))
            and wan_status(self.device_state).get("up_time") is not None
        )

    @property
    def native_value(self) -> int | None:
        from .cloud_helpers import wan_status

        try:
            return int(wan_status(self.device_state).get("up_time"))
        except (TypeError, ValueError):
            return None


class TPLinkCloudBatterySensor(TPLinkCloudDeviceEntity, SensorEntity):
    _attr_translation_key = "cloud_battery"
    _attr_icon = "mdi:battery"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "battery")

    @property
    def available(self) -> bool:
        from .cloud_helpers import battery_percent

        return (
            super().available
            and bool(self.device.get("online"))
            and battery_percent(self.device_state) is not None
        )

    @property
    def native_value(self) -> float | None:
        from .cloud_helpers import battery_percent

        return battery_percent(self.device_state)

    @property
    def extra_state_attributes(self) -> dict:
        from .cloud_helpers import battery_info

        return {"battery": battery_info(self.device_state) or None}

"""Camera entity."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import TPLinkIPCClient
from .const import DOMAIN, DIR_DOWN, DIR_LEFT, DIR_RIGHT, DIR_UP
from .coordinator import TPLinkIPCCoordinator
from .entity import TPLinkIPCEntity, async_register_ipc_device
from .features import dig


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: TPLinkIPCCoordinator = data["coordinator"]
    client: TPLinkIPCClient = data["client"]

    entities: list[Camera] = []
    if client.is_nvr:
        channels = client.list_channels()
        # Parent must exist before channel cameras reference it by device id.
        via_device_id = async_register_ipc_device(hass, entry, client)
        # Host-level entity kept for service targeting / dashboard links
        entities.append(
            TPLinkIPCCamera(
                coordinator, client, entry, "camera", client.rtsp_url, None
            )
        )
        for ch in channels:
            main = ch.get("main_url") or client.rtsp_url
            sub = ch.get("sub_url") or client.rtsp_url_sub
            name = ch.get("name") or f"CH{ch['id']}"
            entities.append(
                TPLinkIPCChannelCamera(
                    coordinator,
                    client,
                    entry,
                    channel_id=ch["id"],
                    playback_channel=ch["playback_channel"],
                    stream_url=main,
                    name=name,
                    key_suffix="",
                    via_device_id=via_device_id,
                )
            )
            if sub and sub != main:
                entities.append(
                    TPLinkIPCChannelCamera(
                        coordinator,
                        client,
                        entry,
                        channel_id=ch["id"],
                        playback_channel=ch["playback_channel"],
                        stream_url=sub,
                        name=f"{name} 子碼流",
                        key_suffix="_sub",
                        via_device_id=via_device_id,
                    )
                )
    else:
        entities.extend(
            [
                TPLinkIPCCamera(
                    coordinator, client, entry, "camera", client.rtsp_url, None
                ),
                TPLinkIPCCamera(
                    coordinator,
                    client,
                    entry,
                    "camera_sub",
                    client.rtsp_url_sub,
                    translation_key="camera_sub",
                ),
            ]
        )

    async_add_entities(entities)


class TPLinkIPCCamera(TPLinkIPCEntity, Camera):
    """Main / sub RTSP camera (single-channel IPC)."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: TPLinkIPCCoordinator,
        client: TPLinkIPCClient,
        entry: ConfigEntry,
        key: str,
        stream_url: str,
        name: str | None = None,
        *,
        translation_key: str | None = None,
    ) -> None:
        TPLinkIPCEntity.__init__(self, coordinator, client, entry, key)
        Camera.__init__(self)
        self._stream_url = stream_url
        if translation_key:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = name

    @property
    def is_on(self) -> bool:
        enabled = dig(
            self.coordinator.data or {}, ("lens_mask", "lens_mask_info", "enabled")
        )
        if enabled is None:
            return True
        return str(enabled).lower() not in ("on", "1", "true")

    async def stream_source(self) -> str | None:
        return self._stream_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        from homeassistant.components.ffmpeg import async_get_image

        return await async_get_image(
            self.hass, self._stream_url, width=width, height=height
        )

    async def async_perform_action(self, action: str) -> None:
        mapping = {
            "LEFT": DIR_LEFT,
            "RIGHT": DIR_RIGHT,
            "UP": DIR_UP,
            "DOWN": DIR_DOWN,
        }
        if action in mapping:
            await self._client.ptz_step(mapping[action])


class TPLinkIPCChannelCamera(CoordinatorEntity[TPLinkIPCCoordinator], Camera):
    """NVR channel camera — live RTSP from attached IPC URL in chm.chn_info."""

    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TPLinkIPCCoordinator,
        client: TPLinkIPCClient,
        entry: ConfigEntry,
        *,
        channel_id: int,
        playback_channel: int,
        stream_url: str,
        name: str,
        key_suffix: str,
        via_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self._client = client
        self._entry = entry
        self._channel_id = channel_id
        self._playback_channel = playback_channel
        self._stream_url = stream_url
        self._is_sub = bool(key_suffix)
        self._attr_name = name
        self._attr_unique_id = f"{entry.unique_id}_ch{channel_id}{key_suffix}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_ch{channel_id}")},
            name=f"{client.device_name} · {name}",
            manufacturer="TP-Link",
            model=client.model,
            sw_version=client.sw_version,
            via_device_id=via_device_id,
            configuration_url=client.base_url,
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "nvr_channel_id": self._channel_id,
            "playback_channel": self._playback_channel,
            "rtsp_url": self._stream_url,
        }

    async def stream_source(self) -> str | None:
        url = self._client.channel_rtsp_url(self._channel_id, sub=self._is_sub)
        return url or self._stream_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        from homeassistant.components.ffmpeg import async_get_image

        src = await self.stream_source()
        if not src:
            return None
        return await async_get_image(self.hass, src, width=width, height=height)

"""Base entities for TP-Link cloud account entry."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud import TPLinkCloudClient
from .cloud_coordinator import TPLinkCloudCoordinator, normalize_mac
from .cloud_helpers import decode_alias, dig
from .const import DOMAIN


def async_register_cloud_account_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    username: str | None,
) -> str:
    """Register the cloud-account device and return its registry id."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=entry.title or f"TP-Link Cloud ({username})",
        manufacturer="TP-Link",
        model="Cloud account",
        configuration_url="https://www.tplinkcloud.com.cn/",
    )
    return device.id


class TPLinkCloudEntity(CoordinatorEntity[TPLinkCloudCoordinator]):
    """Entity bound to the cloud account config entry."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TPLinkCloudCoordinator,
        client: TPLinkCloudClient,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title or f"TP-Link Cloud ({client.username})",
            manufacturer="TP-Link",
            model="Cloud account",
            configuration_url="https://www.tplinkcloud.com.cn/",
        )


class TPLinkCloudDeviceEntity(TPLinkCloudEntity):
    """Entity for one cloud-bound device."""

    def __init__(
        self,
        coordinator: TPLinkCloudCoordinator,
        client: TPLinkCloudClient,
        entry: ConfigEntry,
        device_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator, client, entry, f"{device_id}_{key}")
        self._device_id = device_id
        self._attr_device_info = self._build_device_info()

    def _build_device_info(self) -> DeviceInfo:
        d = self.device
        alias = (
            d.get("alias")
            or d.get("deviceName")
            or decode_alias(dig(self.device_state, ("system", "sys", "dev_alias")))
            or self._device_id[-8:]
        )
        mac = normalize_mac(d.get("mac_norm") or d.get("deviceMac"))
        connections = {("mac", mac)} if mac else set()
        basic = dig(self.device_state, ("device_info", "basic_info")) or {}
        model = (
            d.get("deviceModel")
            or basic.get("device_model")
            or d.get("model")
            or "Cloud device"
        )
        sw = d.get("fwVer") or decode_alias(basic.get("sw_version"))
        parent = dr.async_get(self.coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, self._entry.unique_id or self._entry.entry_id),
            self._entry.entry_id,
        )
        info = DeviceInfo(
            identifiers={(DOMAIN, f"cloud_dev_{self._device_id}")},
            connections=connections,
            name=str(alias),
            manufacturer="TP-Link",
            model=str(model),
            sw_version=str(sw) if sw else None,
        )
        if parent is not None:
            info["via_device_id"] = parent.id
        return info

    @property
    def device(self) -> dict[str, Any]:
        devices = (self.coordinator.data or {}).get("devices") or {}
        return devices.get(self._device_id) or {}

    @property
    def device_state(self) -> dict[str, Any]:
        """Passthrough JSON for this device (must not be named ``state`` — that is HA Entity.state)."""
        st = self.device.get("state")
        return st if isinstance(st, dict) else {}

    @property
    def available(self) -> bool:
        return bool(self.device) and super().available

    def _state_path(self, path: tuple[str, ...]) -> Any:
        return dig(self.device_state, path)

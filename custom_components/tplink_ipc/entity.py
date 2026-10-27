"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import TPLinkIPCClient
from .const import DOMAIN
from .coordinator import TPLinkIPCCoordinator
from .features import dig


def async_register_ipc_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: TPLinkIPCClient,
) -> str:
    """Register the camera/NVR device and return its registry id."""
    mac = client.mac
    connections = {("mac", mac.replace("-", ":").lower())} if mac else set()
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        connections=connections,
        name=client.device_name,
        manufacturer="TP-Link",
        model=client.model,
        sw_version=client.sw_version,
        configuration_url=client.base_url,
    )
    return device.id


class TPLinkIPCEntity(CoordinatorEntity[TPLinkIPCCoordinator]):
    """Base entity bound to one camera config entry."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TPLinkIPCCoordinator,
        client: TPLinkIPCClient,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        mac = client.mac
        connections = {("mac", mac.replace("-", ":").lower())} if mac else set()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            connections=connections,
            name=client.device_name,
            manufacturer="TP-Link",
            model=client.model,
            sw_version=client.sw_version,
            configuration_url=client.base_url,
        )

    def _path_value(self, path: tuple[str, ...]):
        return dig(self.coordinator.data or {}, path)

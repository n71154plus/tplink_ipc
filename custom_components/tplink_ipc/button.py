"""Button entities."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TPLinkIPCClient
from .cloud import TPLinkCloudClient, TPLinkCloudError
from .cloud_coordinator import TPLinkCloudCoordinator
from .cloud_entity import TPLinkCloudDeviceEntity, TPLinkCloudEntity
from .cloud_helpers import is_ipc_family, is_router_family, is_state_pollable
from .const import CONF_CONNECTION_TYPE, CONN_CLOUD, DOMAIN
from .coordinator import TPLinkIPCCoordinator
from .entity import TPLinkIPCEntity

_LOGGER = logging.getLogger(__name__)

_CLOUD_REBOOT = {"method": "do", "system": {"reboot": None}}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get(CONF_CONNECTION_TYPE) == CONN_CLOUD:
        await _async_setup_cloud_buttons(entry, data, async_add_entities)
        return

    coordinator: TPLinkIPCCoordinator = data["coordinator"]
    client: TPLinkIPCClient = data["client"]
    buttons: list[ButtonEntity] = [
        TPLinkIPCActionButton(
            coordinator, client, entry, "reboot", "mdi:restart", client.reboot
        ),
        TPLinkIPCActionButton(
            coordinator,
            client,
            entry,
            "ptz_stop",
            "mdi:stop",
            client.ptz_stop,
        ),
        TPLinkIPCActionButton(
            coordinator,
            client,
            entry,
            "trigger_alarm",
            "mdi:bullhorn",
            client.trigger_alarm,
        ),
        TPLinkIPCActionButton(
            coordinator,
            client,
            entry,
            "doorbell_wakeup",
            "mdi:doorbell-video",
            client.doorbell_wakeup,
        ),
    ]
    if client.has_module("ptz") or "ptz" in (coordinator.data or {}):
        buttons.append(
            TPLinkIPCActionButton(
                coordinator,
                client,
                entry,
                "ptz_calibrate",
                "mdi:compass",
                client.ptz_calibrate,
            )
        )
    # Format buttons only when storage is present (HDD / SD)
    state = coordinator.data or {}
    if (state.get("harddisk_manage") or {}).get("hd_info"):
        buttons.append(
            TPLinkIPCActionButton(
                coordinator,
                client,
                entry,
                "format_disk",
                "mdi:harddisk-remove",
                lambda: client.format_disk("1"),
            )
        )
    if (state.get("sd_manage") or {}).get("sd_info"):
        buttons.append(
            TPLinkIPCActionButton(
                coordinator,
                client,
                entry,
                "format_sd",
                "mdi:sd",
                lambda: client.format_sd("1"),
            )
        )
    async_add_entities(buttons)


async def _async_setup_cloud_buttons(
    entry: ConfigEntry,
    data: dict,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TPLinkCloudCoordinator = data["cloud_coordinator"]
    client: TPLinkCloudClient = data["cloud_client"]
    known: set[str] = set()

    def _make(device_ids: set[str]) -> list[ButtonEntity]:
        out: list[ButtonEntity] = []
        devices = (coordinator.data or {}).get("devices") or {}
        for did in device_ids:
            dev = devices.get(did) or {}
            if is_state_pollable(dev):
                out.append(
                    TPLinkCloudRefreshButton(coordinator, client, entry, did)
                )
            if is_ipc_family(dev) or is_router_family(dev):
                out.append(
                    TPLinkCloudRebootButton(coordinator, client, entry, did)
                )
        return out

    entities: list[ButtonEntity] = [
        TPLinkCloudAccountRefreshButton(coordinator, client, entry),
        *_make(set((coordinator.data or {}).get("devices") or {})),
    ]
    known.update((coordinator.data or {}).get("devices") or {})
    async_add_entities(entities)

    @callback
    def _on_update() -> None:
        current = set((coordinator.data or {}).get("devices") or {})
        new_ids = current - known
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(_make(new_ids))

    entry.async_on_unload(coordinator.async_add_listener(_on_update))


class TPLinkIPCActionButton(TPLinkIPCEntity, ButtonEntity):
    """Action button."""

    def __init__(
        self,
        coordinator: TPLinkIPCCoordinator,
        client: TPLinkIPCClient,
        entry: ConfigEntry,
        key: str,
        icon: str,
        action,
    ) -> None:
        super().__init__(coordinator, client, entry, key)
        self._attr_translation_key = key
        self._attr_icon = icon
        self._action = action

    async def async_press(self) -> None:
        await self._action()


class TPLinkCloudAccountRefreshButton(TPLinkCloudEntity, ButtonEntity):
    """Refresh entire cloud device list + IPC state."""

    _attr_translation_key = "cloud_refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator, client, entry) -> None:
        super().__init__(coordinator, client, entry, "cloud_refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class TPLinkCloudRebootButton(TPLinkCloudDeviceEntity, ButtonEntity):
    """Reboot a cloud-bound IPC/NVR via passthrough."""

    _attr_translation_key = "cloud_reboot"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "reboot")

    @property
    def available(self) -> bool:
        return super().available and bool(self.device.get("online"))

    async def async_press(self) -> None:
        try:
            await self._client.async_passthrough(self._device_id, _CLOUD_REBOOT)
        except TPLinkCloudError as err:
            _LOGGER.error(
                "Cloud reboot failed for %s: %s", self._device_id[-12:], err
            )
            raise


class TPLinkCloudRefreshButton(TPLinkCloudDeviceEntity, ButtonEntity):
    """Force refresh cloud account coordinator (includes this device)."""

    _attr_translation_key = "cloud_device_refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()

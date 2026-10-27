"""Switch entities for APK-mapped features (local + cloud passthrough)."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TPLinkIPCClient
from .cloud import TPLinkCloudClient, TPLinkCloudError
from .cloud_coordinator import TPLinkCloudCoordinator
from .cloud_entity import TPLinkCloudDeviceEntity
from .cloud_helpers import dig, is_doorbell, is_ipc_family, is_router_family
from .const import CONF_CONNECTION_TYPE, CONN_CLOUD, DOMAIN
from .coordinator import TPLinkIPCCoordinator
from .entity import TPLinkIPCEntity
from .features import SWITCHES, SwitchFeature, dig as local_dig

_LOGGER = logging.getLogger(__name__)

# Control-plane switches for cloud (no PTZ / media)
CLOUD_SWITCH_KEYS = {
    "privacy",
    "led",
    "motion",
    "motion_people",
    "motion_vehicle",
    "people_detection",
    "loop_record",
    "msg_push",
}


def _feature_available(client: TPLinkIPCClient, feat: SwitchFeature, data: dict) -> bool:
    if feat.requires and not client.has_module(feat.requires):
        if local_dig(data, feat.path) is None and local_dig(data, feat.path[:-1]) is None:
            return False
    return local_dig(data, feat.path) is not None or local_dig(data, feat.path[:-1]) is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get(CONF_CONNECTION_TYPE) == CONN_CLOUD:
        await _async_setup_cloud_switches(entry, data, async_add_entities)
        return

    coordinator: TPLinkIPCCoordinator = data["coordinator"]
    client: TPLinkIPCClient = data["client"]
    state = coordinator.data or {}
    entities = [
        TPLinkIPCSwitch(coordinator, client, entry, feat)
        for feat in SWITCHES
        if _feature_available(client, feat, state)
    ]
    async_add_entities(entities)


async def _async_setup_cloud_switches(
    entry: ConfigEntry,
    data: dict,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TPLinkCloudCoordinator = data["cloud_coordinator"]
    client: TPLinkCloudClient = data["cloud_client"]
    known_keys: set[tuple[str, str]] = set()
    feats = [f for f in SWITCHES if f.key in CLOUD_SWITCH_KEYS]

    def _make_for_devices(device_ids: set[str]) -> list[SwitchEntity]:
        out: list[SwitchEntity] = []
        devices = (coordinator.data or {}).get("devices") or {}
        for did in device_ids:
            dev = devices.get(did) or {}
            state = dev.get("state") if isinstance(dev.get("state"), dict) else {}
            if is_ipc_family(dev) or is_doorbell(dev):
                for feat in feats:
                    val = dig(state, feat.path)
                    if val in (None, [], {}):
                        continue
                    key = (did, feat.key)
                    if key in known_keys:
                        continue
                    known_keys.add(key)
                    out.append(
                        TPLinkCloudSwitch(coordinator, client, entry, did, feat)
                    )
            if is_router_family(dev):
                # XDR mesh LED uses 0/1 under led.config.mesh_led_enable
                mesh = dig(state, ("led", "config", "mesh_led_enable"))
                key = (did, "mesh_led")
                if mesh is not None and key not in known_keys:
                    known_keys.add(key)
                    out.append(
                        TPLinkCloudMeshLedSwitch(
                            coordinator, client, entry, did
                        )
                    )
        return out

    entities = _make_for_devices(set((coordinator.data or {}).get("devices") or {}))
    if entities:
        async_add_entities(entities)

    @callback
    def _on_update() -> None:
        # New devices or newly available paths after first online poll
        new_ents = _make_for_devices(
            set((coordinator.data or {}).get("devices") or {})
        )
        if new_ents:
            async_add_entities(new_ents)

    entry.async_on_unload(coordinator.async_add_listener(_on_update))


class TPLinkIPCSwitch(TPLinkIPCEntity, SwitchEntity):
    """Boolean feature switch (local)."""

    def __init__(
        self,
        coordinator: TPLinkIPCCoordinator,
        client: TPLinkIPCClient,
        entry: ConfigEntry,
        feature: SwitchFeature,
    ) -> None:
        super().__init__(coordinator, client, entry, feature.key)
        self._feature = feature
        self._attr_translation_key = feature.key
        self._attr_icon = feature.icon

    @property
    def available(self) -> bool:
        return super().available and self._path_value(self._feature.path) is not None

    @property
    def is_on(self) -> bool:
        val = self._path_value(self._feature.path)
        return str(val).lower() in ("on", "1", "true")

    async def async_turn_on(self, **kwargs) -> None:
        if self._feature.key == "timing_reboot":
            await self._client.set_timing_reboot(enabled=True)
        else:
            await self._client.async_apply(self._feature.set_payload(True))
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        if self._feature.key == "timing_reboot":
            await self._client.set_timing_reboot(enabled=False)
        else:
            await self._client.async_apply(self._feature.set_payload(False))
        await self.coordinator.async_request_refresh()


class TPLinkCloudSwitch(TPLinkCloudDeviceEntity, SwitchEntity):
    """Boolean feature via cloud passthrough."""

    def __init__(
        self,
        coordinator: TPLinkCloudCoordinator,
        client: TPLinkCloudClient,
        entry: ConfigEntry,
        device_id: str,
        feature: SwitchFeature,
    ) -> None:
        super().__init__(coordinator, client, entry, device_id, feature.key)
        self._feature = feature
        self._attr_translation_key = feature.key
        self._attr_icon = feature.icon

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self.device.get("online"))
            and self._state_path(self._feature.path) is not None
        )

    @property
    def is_on(self) -> bool:
        val = self._state_path(self._feature.path)
        return str(val).lower() in ("on", "1", "true")

    async def _async_set(self, on: bool) -> None:
        payload = self._feature.set_payload(on)
        try:
            await self._client.async_passthrough(self._device_id, payload)
        except TPLinkCloudError as err:
            _LOGGER.error(
                "Cloud switch %s on %s failed: %s",
                self._feature.key,
                self._device_id[-8:],
                err,
            )
            raise
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class TPLinkCloudMeshLedSwitch(TPLinkCloudDeviceEntity, SwitchEntity):
    """XDR / mesh router LED (mesh_led_enable 0/1)."""

    _attr_translation_key = "cloud_mesh_led"
    _attr_icon = "mdi:led-on"

    def __init__(self, coordinator, client, entry, device_id: str) -> None:
        super().__init__(coordinator, client, entry, device_id, "mesh_led")

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self.device.get("online"))
            and self._state_path(("led", "config", "mesh_led_enable")) is not None
        )

    @property
    def is_on(self) -> bool:
        val = self._state_path(("led", "config", "mesh_led_enable"))
        try:
            return int(val) == 1
        except (TypeError, ValueError):
            return str(val).lower() in ("1", "on", "true")

    async def _async_set(self, on: bool) -> None:
        payload = {
            "method": "set",
            "led": {"config": {"mesh_led_enable": 1 if on else 0}},
        }
        try:
            await self._client.async_passthrough(self._device_id, payload)
        except TPLinkCloudError as err:
            _LOGGER.error("Cloud mesh LED failed: %s", err)
            raise
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

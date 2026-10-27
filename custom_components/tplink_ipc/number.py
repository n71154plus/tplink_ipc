"""Number entities."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TPLinkIPCClient
from .const import DOMAIN
from .coordinator import TPLinkIPCCoordinator
from .entity import TPLinkIPCEntity
from .features import NUMBERS, NumberFeature, dig


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: TPLinkIPCCoordinator = data["coordinator"]
    client: TPLinkIPCClient = data["client"]
    state = coordinator.data or {}
    async_add_entities(
        [
            TPLinkIPCNumber(coordinator, client, entry, feat)
            for feat in NUMBERS
            if dig(state, feat.path) is not None
        ]
    )


class TPLinkIPCNumber(TPLinkIPCEntity, NumberEntity):
    """Numeric setting."""

    _attr_mode = NumberMode.AUTO

    def __init__(
        self,
        coordinator: TPLinkIPCCoordinator,
        client: TPLinkIPCClient,
        entry: ConfigEntry,
        feature: NumberFeature,
    ) -> None:
        super().__init__(coordinator, client, entry, feature.key)
        self._feature = feature
        self._attr_translation_key = feature.key
        self._attr_icon = feature.icon
        self._attr_native_min_value = feature.minimum
        self._attr_native_max_value = feature.maximum
        self._attr_native_step = feature.step
        self._attr_native_unit_of_measurement = feature.unit

    @property
    def native_value(self) -> float | None:
        val = self._path_value(self._feature.path)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self._client.async_apply(self._feature.set_payload(value))
        await self.coordinator.async_request_refresh()

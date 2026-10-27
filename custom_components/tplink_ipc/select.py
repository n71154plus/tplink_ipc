"""Select entities."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TPLinkIPCClient
from .const import DOMAIN
from .coordinator import TPLinkIPCCoordinator
from .entity import TPLinkIPCEntity
from .features import SELECTS, SelectFeature, dig


def _available(client: TPLinkIPCClient, feat: SelectFeature, data: dict) -> bool:
    return dig(data, feat.path) is not None


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
            TPLinkIPCSelect(coordinator, client, entry, feat)
            for feat in SELECTS
            if _available(client, feat, state)
        ]
    )


class TPLinkIPCSelect(TPLinkIPCEntity, SelectEntity):
    """Enum-like setting."""

    def __init__(
        self,
        coordinator: TPLinkIPCCoordinator,
        client: TPLinkIPCClient,
        entry: ConfigEntry,
        feature: SelectFeature,
    ) -> None:
        super().__init__(coordinator, client, entry, feature.key)
        self._feature = feature
        self._attr_translation_key = feature.key
        self._attr_icon = feature.icon
        self._attr_options = list(feature.options)

    @property
    def current_option(self) -> str | None:
        val = self._path_value(self._feature.path)
        return str(val) if val is not None else None

    async def async_select_option(self, option: str) -> None:
        await self._client.async_apply(self._feature.set_payload(option))
        await self.coordinator.async_request_refresh()

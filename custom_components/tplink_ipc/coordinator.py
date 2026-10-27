"""Data update coordinator."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TPLinkIPCClient, TPLinkIPCError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TPLinkIPCCoordinator(DataUpdateCoordinator[dict]):
    """Poll full device state from local stok API."""

    def __init__(self, hass: HomeAssistant, client: TPLinkIPCClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )
        self.client = client
        # channel(playback index) -> YYYYMMDD list
        self.recording_days_by_channel: dict[int, list[str]] = {}
        self.recording_days_error: str | None = None
        # Per playback channel (NVR multi-cam shares one coordinator)
        self.last_timeline_by_channel: dict[int, dict[str, Any]] = {}
        self.last_playback_by_channel: dict[int, dict[str, Any]] = {}
        self.last_timeline: dict[str, Any] = {}
        self.last_playback: dict[str, Any] = {}
        self._last_days_refresh: datetime | None = None

    @property
    def recording_days(self) -> list[str]:
        """Days for channel 0 (IPC) or union for sensors that expect a flat list."""
        if 0 in self.recording_days_by_channel:
            return self.recording_days_by_channel[0]
        if not self.recording_days_by_channel:
            return []
        merged: set[str] = set()
        for days in self.recording_days_by_channel.values():
            merged.update(days)
        return sorted(merged)

    async def async_refresh_recording_days(
        self, *, force: bool = False, months_back: int = 2
    ) -> dict[int, list[str]]:
        """Refresh APK-style recording calendar for all relevant channels."""
        now = dt_util.utcnow()
        if (
            not force
            and self._last_days_refresh
            and now - self._last_days_refresh < timedelta(minutes=30)
        ):
            return self.recording_days_by_channel

        end = dt_util.now()
        start = end - timedelta(days=max(30, months_back * 31))
        start_s, end_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        channels = [0]
        if self.client.is_nvr:
            chans = self.client.list_channels()
            if chans:
                channels = [c["playback_channel"] for c in chans]
            else:
                channels = list(range(self.client.max_channels))

        errors: list[str] = []
        for ch in channels:
            try:
                days = await self.client.search_recording_days(
                    start_s, end_s, channel=ch
                )
                self.recording_days_by_channel[ch] = days
            except TPLinkIPCError as err:
                errors.append(f"ch{ch}:{err}")
                _LOGGER.debug("recording days ch=%s failed: %s", ch, err)

        self.recording_days_error = "; ".join(errors) if errors else None
        self._last_days_refresh = now
        return self.recording_days_by_channel

    async def async_load_timeline(
        self, date: str, *, channel: int = 0
    ) -> dict[str, Any]:
        result = await self.client.search_day_timeline(date, channel=channel)
        self.last_timeline_by_channel[int(channel)] = result
        self.last_timeline = result
        self.async_set_updated_data(self.data or {})
        return result

    def set_last_playback(self, channel: int, payload: dict[str, Any]) -> None:
        self.last_playback_by_channel[int(channel)] = payload
        self.last_playback = payload
        self.async_set_updated_data(self.data or {})

    async def _async_update_data(self) -> dict:
        try:
            data = await self.client.async_fetch_state()
        except TPLinkIPCError as err:
            raise UpdateFailed(str(err)) from err
        storage = self.client.storage_info()
        if storage and storage.get("status") == "normal":
            await self.async_refresh_recording_days(force=False)
        elif self.client.is_nvr:
            await self.async_refresh_recording_days(force=False)
        return data

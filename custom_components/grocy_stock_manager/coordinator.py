"""Inventory update coordinator for Grocy Stock Manager."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GrocyApiError
from .const import DOMAIN
from .inventory import GrocyInventory, InventorySnapshot

_UPDATE_INTERVAL = timedelta(minutes=5)
_LOGGER = logging.getLogger(__name__)


class GrocyInventoryCoordinator(DataUpdateCoordinator[InventorySnapshot]):
    """Keep one shared, location-aware Grocy inventory snapshot current."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        inventory: GrocyInventory,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} inventory",
            update_interval=_UPDATE_INTERVAL,
        )
        self._inventory = inventory

    async def _async_update_data(self) -> InventorySnapshot:
        """Fetch current inventory from Grocy."""
        try:
            return await self._inventory.async_snapshot()
        except GrocyApiError as err:
            raise UpdateFailed("Unable to refresh Grocy inventory") from err

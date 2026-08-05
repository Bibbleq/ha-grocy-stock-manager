"""Sensor platform for Grocy Stock Manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GrocyInventoryCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import GrocyStockManagerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the location-aware inventory sensor."""
    async_add_entities([GrocyInventorySensor(entry)])


class GrocyInventorySensor(CoordinatorEntity[GrocyInventoryCoordinator], SensorEntity):
    """Expose the current Grocy inventory for dashboards and automations."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:warehouse"
    _attr_translation_key = "inventory"

    def __init__(self, entry: GrocyStockManagerConfigEntry) -> None:
        """Initialise the inventory sensor."""
        coordinator = entry.runtime_data.coordinator
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_inventory"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Grocy Stock Manager",
            manufacturer="Grocy",
            configuration_url=entry.runtime_data.client.base_url,
        )

    @property
    def native_value(self) -> int:
        """Return the number of distinct products currently in stock."""
        return self.coordinator.data.stocked_product_count

    @property
    def extra_state_attributes(self) -> dict:
        """Return product and shelf breakdowns from one snapshot."""
        return self.coordinator.data.as_attributes()

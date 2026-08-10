"""Sensor platform for Grocy Stock Manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GrocyInventoryCoordinator
from .journal import is_undoable_result

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import GrocyStockManagerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the location-aware inventory sensor."""
    async_add_entities(
        [GrocyInventorySensor(entry), GrocyStockManagerStatusSensor(entry)]
    )


def _device_info(entry: GrocyStockManagerConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Grocy Stock Manager",
        manufacturer="Grocy",
        configuration_url=entry.runtime_data.client.base_url,
    )


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
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        """Return the number of distinct products currently in stock."""
        return self.coordinator.data.stocked_product_count

    @property
    def extra_state_attributes(self) -> dict:
        """Return product and shelf breakdowns from one snapshot."""
        return self.coordinator.data.as_attributes()


_ACTIVITY_FIELDS = (
    "request_id",
    "recorded_at",
    "operation",
    "outcome",
    "success",
    "source",
    "product_id",
    "product_name",
    "location_id",
    "location_name",
    "amount",
    "stock_before",
    "stock_after",
    "replayed",
    "requires_reconciliation",
    "uncertainty_reason",
    "error_code",
    "undo_of",
    "undone_by",
    "reconciled_at",
    "reconciliation_note",
)


def _compact_activity(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _ACTIVITY_FIELDS if key in item}


class GrocyStockManagerStatusSensor(
    CoordinatorEntity[GrocyInventoryCoordinator], SensorEntity
):
    """Expose health, recent verified activity and recovery requirements."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:shield-check"
    _attr_translation_key = "status"

    def __init__(self, entry: GrocyStockManagerConfigEntry) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str:
        """Return a small state suitable for a production health latch."""
        if any(
            item.get("requires_reconciliation")
            for item in self._entry.runtime_data.journal.snapshot(limit=256)
        ):
            return "attention"
        return "ready"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return bounded activity and pending-confirmation diagnostics."""
        all_activity = self._entry.runtime_data.journal.snapshot(limit=256)
        reconciliation = [
            _compact_activity(item)
            for item in all_activity
            if item.get("requires_reconciliation")
        ]
        recent = [_compact_activity(item) for item in all_activity[:10]]
        last_transaction = next(
            (
                item
                for item in recent
                if item.get("recorded_at") and is_undoable_result(item)
            ),
            None,
        )
        return {
            "api_connected": self.coordinator.last_update_success,
            "recent_activity": recent,
            "last_transaction": last_transaction,
            "reconciliation_required": reconciliation,
            "reconciliation_required_count": len(reconciliation),
            "pending_voice_confirmations": (
                self._entry.runtime_data.pending_voice.snapshot()
            ),
        }

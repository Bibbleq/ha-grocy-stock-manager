"""Diagnostics support for Grocy Stock Manager."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import GrocyStockManagerConfigEntry
from .const import CONF_TAVILY_API_KEY


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics with credentials removed."""
    return {
        "config_entry": async_redact_data(
            dict(entry.data), {CONF_API_KEY, CONF_TAVILY_API_KEY}
        ),
        "system_info": entry.runtime_data.system_info,
        "pending_product_identifications": (
            entry.runtime_data.pending_identifications.pending_snapshot()
        ),
    }

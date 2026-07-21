"""Diagnostics support for Grocy Stock Manager."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import GrocyStockManagerConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics with credentials removed."""
    return {
        "config_entry": async_redact_data(dict(entry.data), {CONF_API_KEY}),
        "system_info": entry.runtime_data.system_info,
    }

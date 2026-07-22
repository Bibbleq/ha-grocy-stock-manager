"""Grocy Stock Manager integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    GrocyApiClient,
    GrocyApiError,
    GrocyCannotConnectError,
    GrocyInvalidAuthError,
)
from .resolver import GrocyProductResolver
from .services import async_setup_services, async_unload_services


@dataclass(slots=True)
class GrocyStockManagerRuntimeData:
    """Runtime data held for a configured Grocy endpoint."""

    client: GrocyApiClient
    resolver: GrocyProductResolver
    system_info: dict[str, Any]


type GrocyStockManagerConfigEntry = ConfigEntry[GrocyStockManagerRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> bool:
    """Set up Grocy Stock Manager from a config entry."""
    session = async_get_clientsession(
        hass,
        verify_ssl=entry.data[CONF_VERIFY_SSL],
    )
    client = GrocyApiClient(
        session,
        entry.data[CONF_URL],
        entry.data[CONF_API_KEY],
    )

    try:
        system_info = await client.async_get_system_info()
    except GrocyInvalidAuthError as err:
        raise ConfigEntryAuthFailed from err
    except (GrocyCannotConnectError, GrocyApiError) as err:
        raise ConfigEntryNotReady from err

    entry.runtime_data = GrocyStockManagerRuntimeData(
        client=client,
        resolver=GrocyProductResolver(client),
        system_info=dict(system_info),
    )
    async_setup_services(hass, entry)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> bool:
    """Unload a Grocy Stock Manager config entry."""
    async_unload_services(hass)
    return True

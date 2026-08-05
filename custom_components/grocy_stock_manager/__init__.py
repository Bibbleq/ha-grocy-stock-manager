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
from .catalogue import GrocyCatalogueManager
from .journal import TransactionJournal
from .resolver import GrocyProductResolver
from .services import async_setup_services, async_unload_services
from .transactions import GrocyTransactionManager
from .voice import (
    GrocyVoiceAliases,
    GrocyVoiceManager,
    GrocyVoiceResolver,
)


@dataclass(slots=True)
class GrocyStockManagerRuntimeData:
    """Runtime data held for a configured Grocy endpoint."""

    client: GrocyApiClient
    resolver: GrocyProductResolver
    transactions: GrocyTransactionManager
    catalogue: GrocyCatalogueManager
    voice_aliases: GrocyVoiceAliases
    voice_resolver: GrocyVoiceResolver
    voice: GrocyVoiceManager
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

    resolver = GrocyProductResolver(client)
    journal = TransactionJournal(hass)
    await journal.async_load()
    voice_aliases = GrocyVoiceAliases(client)
    transactions = GrocyTransactionManager(client, resolver, journal)
    voice_resolver = GrocyVoiceResolver(client, resolver, voice_aliases)
    entry.runtime_data = GrocyStockManagerRuntimeData(
        client=client,
        resolver=resolver,
        transactions=transactions,
        catalogue=GrocyCatalogueManager(client, resolver),
        voice_aliases=voice_aliases,
        voice_resolver=voice_resolver,
        voice=GrocyVoiceManager(
            voice_resolver,
            resolver,
            transactions,
            voice_aliases,
        ),
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

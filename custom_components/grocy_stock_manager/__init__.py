"""Grocy Stock Manager integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
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
from .coordinator import GrocyInventoryCoordinator
from .identification import (
    ProductIdentificationManager,
    ProductIdentificationStore,
)
from .inventory import GrocyInventory
from .journal import TransactionJournal
from .merges import GrocyProductMergeManager
from .pending import VoiceConfirmationStore
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
    journal: TransactionJournal
    catalogue: GrocyCatalogueManager
    voice_aliases: GrocyVoiceAliases
    voice_resolver: GrocyVoiceResolver
    voice: GrocyVoiceManager
    merges: GrocyProductMergeManager
    pending_voice: VoiceConfirmationStore
    pending_identifications: ProductIdentificationStore
    identification: ProductIdentificationManager
    coordinator: GrocyInventoryCoordinator
    system_info: dict[str, Any]


type GrocyStockManagerConfigEntry = ConfigEntry[GrocyStockManagerRuntimeData]

PLATFORMS = [Platform.SENSOR]


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
    pending_voice = VoiceConfirmationStore(hass)
    await pending_voice.async_load()
    pending_identifications = ProductIdentificationStore(hass)
    await pending_identifications.async_load()
    voice_aliases = GrocyVoiceAliases(client)
    identification = ProductIdentificationManager(
        hass,
        entry,
        pending_identifications,
        journal,
        voice_aliases,
    )
    transactions = GrocyTransactionManager(client, resolver, journal)
    voice_resolver = GrocyVoiceResolver(client, resolver, voice_aliases)
    coordinator = GrocyInventoryCoordinator(hass, entry, GrocyInventory(client))
    await coordinator.async_config_entry_first_refresh()
    runtime_data = GrocyStockManagerRuntimeData(
        client=client,
        resolver=resolver,
        transactions=transactions,
        journal=journal,
        catalogue=GrocyCatalogueManager(client, resolver),
        voice_aliases=voice_aliases,
        voice_resolver=voice_resolver,
        voice=GrocyVoiceManager(
            voice_resolver,
            resolver,
            transactions,
            voice_aliases,
            pending_voice,
        ),
        merges=GrocyProductMergeManager(
            client,
            resolver,
            transactions,
            voice_aliases,
            journal,
        ),
        coordinator=coordinator,
        pending_voice=pending_voice,
        pending_identifications=pending_identifications,
        identification=identification,
        system_info=dict(system_info),
    )
    entry.runtime_data = runtime_data
    runtime_data.identification.async_resume()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass, entry)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> bool:
    """Unload a Grocy Stock Manager config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        async_unload_services(hass)
    return unload_ok

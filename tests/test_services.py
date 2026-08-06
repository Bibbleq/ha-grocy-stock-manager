"""Tests for Home Assistant action registration and responses."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.grocy_stock_manager.const import (
    ATTR_AMOUNT,
    ATTR_BARCODE,
    ATTR_PRODUCT_ID,
    ATTR_REQUEST_ID,
    ATTR_SOURCE,
    DOMAIN,
    SERVICE_CONFIRM_PRODUCT,
    SERVICE_CONFIRM_VOICE_TRANSACTION,
    SERVICE_LEARN_PRODUCT_ALIAS,
    SERVICE_LIST_PRODUCT_ALIASES,
    SERVICE_LOOKUP,
    SERVICE_REMOVE_PRODUCT_ALIAS,
    SERVICE_RESOLVE_PRODUCT_PHRASE,
    SERVICE_VOICE_TRANSACTION,
)
from custom_components.grocy_stock_manager.inventory import (
    GrocyInventory,
    InventorySnapshot,
)
from custom_components.grocy_stock_manager.models import (
    ProductDetails,
    ProductLookupResult,
    parse_stock_locations,
)
from custom_components.grocy_stock_manager.resolver import GrocyProductResolver
from custom_components.grocy_stock_manager.services import _async_mutate
from custom_components.grocy_stock_manager.transactions import (
    TransactionLocationAmbiguousError,
)

from .test_models import PRODUCT_DETAILS, STOCK_LOCATIONS


async def test_expected_mutation_rejection_returns_structured_response(
    hass: HomeAssistant,
) -> None:
    """An ambiguous shelf is API-safe, explicit, and never reported as success."""
    lookup_result = ProductLookupResult(
        lookup_type="product_id",
        lookup_value=1,
        product=ProductDetails.from_payload(PRODUCT_DETAILS),
        stock_locations=parse_stock_locations(STOCK_LOCATIONS),
    )
    resolver = SimpleNamespace(
        async_lookup_by_product_id=AsyncMock(return_value=lookup_result)
    )
    transactions = SimpleNamespace(
        async_execute=AsyncMock(side_effect=TransactionLocationAmbiguousError)
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            resolver=resolver,
            transactions=transactions,
        )
    )
    call = SimpleNamespace(
        hass=hass,
        data={
            ATTR_PRODUCT_ID: 1,
            ATTR_AMOUNT: Decimal("1"),
            ATTR_REQUEST_ID: "voice-ambiguous-1",
            ATTR_SOURCE: "voice",
        },
    )

    response = await _async_mutate(entry, call, "consume")

    assert response["success"] is False
    assert response["outcome"] == "rejected"
    assert response["stock_changed"] is False
    assert response["error_code"] == "location_ambiguous"
    assert response["requires_reconciliation"] is False
    assert response["product_id"] == 1
    assert response["stock_total"] == 3.0
    assert response["stock_locations"] == [
        {
            "location_id": 12,
            "location_name": "Garage A",
            "amount": 2.0,
        },
        {
            "location_id": 13,
            "location_name": "Garage Z",
            "amount": 1.0,
        },
    ]


async def test_lookup_action_returns_data_and_unloads(hass: HomeAssistant) -> None:
    """The response-only action follows the config-entry lifecycle."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="http://grocy.local:9192",
        data={
            CONF_URL: "http://grocy.local:9192",
            CONF_API_KEY: "secret",
            CONF_VERIFY_SSL: True,
        },
        unique_id="http://grocy.local:9192",
    )
    entry.add_to_hass(hass)

    lookup_result = ProductLookupResult(
        lookup_type="barcode",
        lookup_value="04260066669009",
        product=ProductDetails.from_payload(PRODUCT_DETAILS),
        stock_locations=parse_stock_locations(STOCK_LOCATIONS),
    )

    with (
        patch(
            "custom_components.grocy_stock_manager.api.GrocyApiClient."
            "async_get_system_info",
            AsyncMock(return_value={"grocy_version": {"Version": "4.6.0"}}),
        ),
        patch.object(
            GrocyProductResolver,
            "async_lookup_by_barcode",
            AsyncMock(return_value=lookup_result),
        ),
        patch.object(
            GrocyInventory,
            "async_snapshot",
            AsyncMock(return_value=InventorySnapshot(products=(), locations=())),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, SERVICE_LOOKUP)
        assert hass.services.has_service(DOMAIN, SERVICE_CONFIRM_PRODUCT)
        assert hass.services.has_service(DOMAIN, SERVICE_RESOLVE_PRODUCT_PHRASE)
        assert hass.services.has_service(DOMAIN, SERVICE_VOICE_TRANSACTION)
        assert hass.services.has_service(DOMAIN, SERVICE_CONFIRM_VOICE_TRANSACTION)
        assert hass.services.has_service(DOMAIN, SERVICE_LEARN_PRODUCT_ALIAS)
        assert hass.services.has_service(DOMAIN, SERVICE_REMOVE_PRODUCT_ALIAS)
        assert hass.services.has_service(DOMAIN, SERVICE_LIST_PRODUCT_ALIASES)
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_LOOKUP,
            {ATTR_BARCODE: "04260066669009"},
            blocking=True,
            return_response=True,
        )

    assert response is not None
    assert response["response_version"] == 1
    assert response["product_id"] == 1
    assert response["product_name"] == "Cat litter (Synthetic Grey)"
    assert response["stock_total"] == 3.0

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, SERVICE_LOOKUP)
    assert not hass.services.has_service(DOMAIN, SERVICE_CONFIRM_PRODUCT)
    assert not hass.services.has_service(DOMAIN, SERVICE_RESOLVE_PRODUCT_PHRASE)
    assert not hass.services.has_service(DOMAIN, SERVICE_VOICE_TRANSACTION)
    assert not hass.services.has_service(DOMAIN, SERVICE_CONFIRM_VOICE_TRANSACTION)
    assert not hass.services.has_service(DOMAIN, SERVICE_LEARN_PRODUCT_ALIAS)
    assert not hass.services.has_service(DOMAIN, SERVICE_REMOVE_PRODUCT_ALIAS)
    assert not hass.services.has_service(DOMAIN, SERVICE_LIST_PRODUCT_ALIASES)

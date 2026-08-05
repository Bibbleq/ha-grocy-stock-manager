"""Tests for Home Assistant lookup action registration."""

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.grocy_stock_manager.const import (
    ATTR_BARCODE,
    DOMAIN,
    SERVICE_CONFIRM_PRODUCT,
    SERVICE_LOOKUP,
)
from custom_components.grocy_stock_manager.models import (
    ProductDetails,
    ProductLookupResult,
    parse_stock_locations,
)
from custom_components.grocy_stock_manager.resolver import GrocyProductResolver

from .test_models import PRODUCT_DETAILS, STOCK_LOCATIONS


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
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, SERVICE_LOOKUP)
        assert hass.services.has_service(DOMAIN, SERVICE_CONFIRM_PRODUCT)
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

"""Read-only Grocy product resolution."""

from __future__ import annotations

from .api import GrocyApiClient
from .models import ProductDetails, ProductLookupResult, parse_stock_locations


class GrocyProductResolver:
    """Resolve Grocy products into one stable response model."""

    def __init__(self, client: GrocyApiClient) -> None:
        """Initialise the resolver."""
        self._client = client

    async def _async_complete_lookup(
        self,
        details_payload: object,
        *,
        lookup_type: str,
        lookup_value: str | int,
    ) -> ProductLookupResult:
        product = ProductDetails.from_payload(details_payload)
        raw_locations = await self._client.async_get_product_stock_locations(
            product.id
        )
        return ProductLookupResult(
            lookup_type=lookup_type,
            lookup_value=lookup_value,
            product=product,
            stock_locations=parse_stock_locations(raw_locations),
        )

    async def async_lookup_by_barcode(self, barcode: str) -> ProductLookupResult:
        """Resolve one exact barcode, preserving leading zeroes."""
        value = barcode.strip()
        details = await self._client.async_get_product_by_barcode(value)
        return await self._async_complete_lookup(
            details,
            lookup_type="barcode",
            lookup_value=value,
        )

    async def async_lookup_by_product_id(
        self, product_id: int
    ) -> ProductLookupResult:
        """Resolve one exact Grocy product id."""
        details = await self._client.async_get_product_by_id(product_id)
        return await self._async_complete_lookup(
            details,
            lookup_type="product_id",
            lookup_value=product_id,
        )

    async def async_lookup_by_product_name(
        self, product_name: str
    ) -> ProductLookupResult:
        """Resolve one exact Grocy product name."""
        value = product_name.strip()
        details = await self._client.async_get_product_by_name(value)
        return await self._async_complete_lookup(
            details,
            lookup_type="product_name",
            lookup_value=value,
        )

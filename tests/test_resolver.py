"""Tests for read-only product resolution."""

import pytest

from custom_components.grocy_stock_manager.api import (
    GrocyApiClient,
    GrocyInvalidResponseError,
)
from custom_components.grocy_stock_manager.resolver import GrocyProductResolver

from .test_api import FakeResponse, FakeSession
from .test_models import PRODUCT_DETAILS, STOCK_LOCATIONS


async def test_lookup_by_barcode_reads_product_and_current_locations() -> None:
    """A barcode lookup composes the two authoritative Grocy reads."""
    session = FakeSession(
        FakeResponse(200, PRODUCT_DETAILS),
        FakeResponse(200, STOCK_LOCATIONS),
    )
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")
    resolver = GrocyProductResolver(client)

    result = await resolver.async_lookup_by_barcode(" 04260066669009 ")

    assert result.lookup_value == "04260066669009"
    assert result.product.id == 1
    assert result.matched_barcode is not None
    assert result.matched_barcode.barcode == "04260066669009"
    assert result.matched_barcode.amount is not None
    assert len(result.stock_locations) == 2
    assert session.requests[0][0].endswith(
        "/api/stock/products/by-barcode/04260066669009"
    )
    assert session.requests[1][0].endswith("/api/stock/products/1/locations")


async def test_lookup_by_product_id_has_no_matched_barcode() -> None:
    """Non-barcode lookup paths do not imply which barcode was scanned."""
    session = FakeSession(
        FakeResponse(200, PRODUCT_DETAILS),
        FakeResponse(200, STOCK_LOCATIONS),
    )
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")
    resolver = GrocyProductResolver(client)

    result = await resolver.async_lookup_by_product_id(1)

    assert result.matched_barcode is None


async def test_barcode_lookup_fails_when_grocy_omits_the_mapping() -> None:
    """A contradictory Grocy barcode response is never silently accepted."""
    details_without_barcodes = {**PRODUCT_DETAILS, "product_barcodes": []}
    session = FakeSession(FakeResponse(200, details_without_barcodes))
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")
    resolver = GrocyProductResolver(client)

    with pytest.raises(GrocyInvalidResponseError):
        await resolver.async_lookup_by_barcode("04260066669009")

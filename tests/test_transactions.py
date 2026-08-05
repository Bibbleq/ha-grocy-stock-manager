"""Tests for verified, location-aware Grocy transactions."""

from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from custom_components.grocy_stock_manager.models import (
    ProductDetails,
    ProductLookupResult,
    parse_stock_locations,
)
from custom_components.grocy_stock_manager.transactions import (
    GrocyTransactionManager,
    TransactionInsufficientStockError,
    TransactionLocationAmbiguousError,
    TransactionRequestConflictError,
)

from .test_models import PRODUCT_DETAILS, STOCK_LOCATIONS


class MemoryJournal:
    """In-memory journal implementing the transaction-manager contract."""

    def __init__(self) -> None:
        self.records = {}

    async def async_get(self, request_id):
        return self.records.get(request_id)

    async def async_record(self, request_id, fingerprint, result):
        self.records[request_id] = {
            "request_id": request_id,
            "fingerprint": fingerprint,
            "result": result,
        }


def _lookup(stock_locations=STOCK_LOCATIONS) -> ProductLookupResult:
    return ProductLookupResult(
        lookup_type="product_id",
        lookup_value=1,
        product=ProductDetails.from_payload(PRODUCT_DETAILS),
        stock_locations=parse_stock_locations(stock_locations),
    )


def _lookup_after(location_id: int, amount: str) -> ProductLookupResult:
    payload = deepcopy(PRODUCT_DETAILS)
    payload["stock_amount"] = amount
    location_name = "Garage A" if location_id == 12 else "Garage Z"
    return ProductLookupResult(
        lookup_type="product_id",
        lookup_value=1,
        product=ProductDetails.from_payload(payload),
        stock_locations=parse_stock_locations(
            [
                {
                    "product_id": "1",
                    "amount": amount,
                    "location_id": str(location_id),
                    "location_name": location_name,
                }
            ]
        ),
    )


async def test_add_uses_default_location_verifies_and_deduplicates() -> None:
    """A repeated request ID returns its recorded response without a second POST."""
    client = AsyncMock()
    client.async_add_product.return_value = [{"id": "101"}]
    resolver = AsyncMock()
    resolver.async_lookup_by_product_id.side_effect = [
        _lookup(),
        _lookup_after(12, "3.0"),
    ]
    manager = GrocyTransactionManager(client, resolver, MemoryJournal())

    first = await manager.async_execute(
        "add",
        _lookup(),
        amount=Decimal("1"),
        request_id="scanner-boot-1",
        location_id=None,
        location_name=None,
        source="garage_scanner",
    )
    replay = await manager.async_execute(
        "add",
        _lookup(),
        amount=Decimal("1"),
        request_id="scanner-boot-1",
        location_id=None,
        location_name=None,
        source="garage_scanner",
    )

    assert first["outcome"] == "committed"
    assert first["stock_before"] == 2.0
    assert first["stock_after"] == 3.0
    assert replay["replayed"] is True
    client.async_add_product.assert_awaited_once_with(
        1, amount="1", location_id=12
    )


async def test_consume_infers_the_only_stocked_location() -> None:
    """A shelf is unnecessary when the product exists in only one place."""
    one_location = [
        {
            "product_id": "1",
            "amount": "2",
            "location_id": "13",
            "location_name": "Garage Z",
        }
    ]
    client = AsyncMock()
    client.async_consume_product.return_value = [{"id": "102"}]
    resolver = AsyncMock()
    resolver.async_lookup_by_product_id.side_effect = [
        _lookup(one_location),
        _lookup_after(13, "1"),
    ]
    manager = GrocyTransactionManager(client, resolver, MemoryJournal())

    result = await manager.async_execute(
        "consume",
        _lookup(one_location),
        amount=Decimal("1"),
        request_id="voice-1",
        location_id=None,
        location_name=None,
        source="voice",
    )

    assert result["success"] is True
    assert result["location_id"] == 13
    client.async_consume_product.assert_awaited_once_with(
        1, amount="1", location_id=13
    )


async def test_consume_rejects_ambiguous_locations() -> None:
    """Multiple sufficient shelves without a valid default fail closed."""
    payload = deepcopy(PRODUCT_DETAILS)
    payload["product"]["default_consume_location_id"] = None
    lookup = ProductLookupResult(
        lookup_type="product_id",
        lookup_value=1,
        product=ProductDetails.from_payload(payload),
        stock_locations=parse_stock_locations(STOCK_LOCATIONS),
    )
    resolver = AsyncMock()
    resolver.async_lookup_by_product_id.return_value = lookup
    manager = GrocyTransactionManager(AsyncMock(), resolver, MemoryJournal())

    with pytest.raises(TransactionLocationAmbiguousError):
        await manager.async_execute(
            "consume",
            lookup,
            amount=Decimal("1"),
            request_id="voice-2",
            location_id=None,
            location_name=None,
            source="voice",
        )


async def test_consume_rejects_insufficient_explicit_location() -> None:
    """An explicit shelf still cannot consume more than it contains."""
    client = AsyncMock()
    client.async_get_locations.return_value = [
        {"id": "13", "name": "Garage Z"}
    ]
    resolver = AsyncMock()
    resolver.async_lookup_by_product_id.return_value = _lookup()
    manager = GrocyTransactionManager(client, resolver, MemoryJournal())

    with pytest.raises(TransactionInsufficientStockError):
        await manager.async_execute(
            "consume",
            _lookup(),
            amount=Decimal("2"),
            request_id="tablet-1",
            location_id=13,
            location_name=None,
            source="tablet",
        )


async def test_request_id_cannot_be_reused_for_different_work() -> None:
    """Idempotency keys bind permanently to their original request fingerprint."""
    journal = MemoryJournal()
    journal.records["same"] = {
        "request_id": "same",
        "fingerprint": "different",
        "result": {},
    }
    manager = GrocyTransactionManager(AsyncMock(), AsyncMock(), journal)

    with pytest.raises(TransactionRequestConflictError):
        await manager.async_execute(
            "add",
            _lookup(),
            amount=Decimal("1"),
            request_id="same",
            location_id=None,
            location_name=None,
            source="tablet",
        )

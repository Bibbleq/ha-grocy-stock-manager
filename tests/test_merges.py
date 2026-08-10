"""Tests for guarded native Grocy product consolidation."""

from copy import deepcopy
from unittest.mock import AsyncMock

from custom_components.grocy_stock_manager.api import GrocyNotFoundError
from custom_components.grocy_stock_manager.merges import GrocyProductMergeManager
from custom_components.grocy_stock_manager.models import (
    ProductDetails,
    ProductLookupResult,
    parse_stock_locations,
)
from custom_components.grocy_stock_manager.transactions import GrocyTransactionManager

from .test_models import PRODUCT_DETAILS
from .test_transactions import MemoryJournal


def _lookup(
    product_id: int,
    name: str,
    barcode: str,
    amount: str,
) -> ProductLookupResult:
    payload = deepcopy(PRODUCT_DETAILS)
    payload["product"]["id"] = str(product_id)
    payload["product"]["name"] = name
    payload["product_barcodes"] = [
        {
            "product_id": str(product_id),
            "barcode": barcode,
            "qu_id": "4",
            "amount": "1",
        }
    ]
    payload["stock_amount"] = amount
    locations = []
    if amount != "0":
        locations.append(
            {
                "product_id": str(product_id),
                "amount": amount,
                "location_id": "14",
                "location_name": "Garage L4",
            }
        )
    return ProductLookupResult(
        lookup_type="product_id",
        lookup_value=product_id,
        product=ProductDetails.from_payload(payload),
        stock_locations=parse_stock_locations(locations),
    )


async def test_dry_run_returns_complete_plan_without_writes() -> None:
    """A dry run exposes every material consequence and changes nothing."""
    target = _lookup(97, "sherry", "TARGET", "1")
    source = _lookup(98, "Croft sherry", "SOURCE", "1")
    client = AsyncMock()
    client.async_get_product_userfields.side_effect = [
        {"voice_aliases": "sherry"},
        {"voice_aliases": "croft sherry\npale sherry"},
    ]
    client.async_get_products.return_value = [
        {"id": 97, "name": "sherry", "active": 1},
        {"id": 98, "name": "Croft sherry", "active": 1},
    ]
    resolver = AsyncMock()
    resolver.async_lookup_by_product_id.side_effect = [target, source]
    aliases = AsyncMock()
    aliases.async_index.return_value = {
        "sherry": frozenset({97}),
        "croft sherry": frozenset({98}),
        "pale sherry": frozenset({98}),
    }
    journal = MemoryJournal()
    transactions = GrocyTransactionManager(client, resolver, journal)
    manager = GrocyProductMergeManager(
        client, resolver, transactions, aliases, journal
    )

    result = await manager.async_execute(
        product_id_to_keep=97,
        product_id_to_remove=98,
        canonical_name="Sherry",
        request_id="merge-sherry-dry-run",
        dry_run=True,
    )

    assert result["outcome"] == "planned"
    assert result["stock_changed"] is False
    assert result["expected"]["stock_total"] == 2.0
    assert result["expected"]["barcodes"] == ["SOURCE", "TARGET"]
    assert result["expected"]["voice_aliases"] == [
        "croft sherry",
        "pale sherry",
        "sherry",
    ]
    client.async_merge_products.assert_not_awaited()
    client.async_set_product_userfield.assert_not_awaited()


async def test_execute_uses_native_merge_preserves_aliases_and_verifies() -> None:
    """The irreversible step is Grocy-native and the full result is read back."""
    target = _lookup(97, "sherry", "TARGET", "1")
    source = _lookup(98, "Croft sherry", "SOURCE", "1")
    merged_old_name = _lookup(97, "sherry", "TARGET", "2")
    merged_old_name = ProductLookupResult(
        lookup_type="product_id",
        lookup_value=97,
        product=ProductDetails.from_payload(
            {
                **PRODUCT_DETAILS,
                "product": {"id": "97", "name": "sherry"},
                "product_barcodes": [
                    {"barcode": "TARGET", "qu_id": "4", "amount": "1"},
                    {"barcode": "SOURCE", "qu_id": "4", "amount": "1"},
                ],
                "stock_amount": "2",
            }
        ),
        stock_locations=parse_stock_locations(
            [
                {
                    "product_id": "97",
                    "amount": "2",
                    "location_id": "14",
                    "location_name": "Garage L4",
                }
            ]
        ),
    )
    merged = ProductLookupResult(
        lookup_type="product_id",
        lookup_value=97,
        product=ProductDetails.from_payload(
            {
                **PRODUCT_DETAILS,
                "product": {"id": "97", "name": "Sherry"},
                "product_barcodes": [
                    {"barcode": "TARGET", "qu_id": "4", "amount": "1"},
                    {"barcode": "SOURCE", "qu_id": "4", "amount": "1"},
                ],
                "stock_amount": "2",
            }
        ),
        stock_locations=merged_old_name.stock_locations,
    )
    client = AsyncMock()
    client.async_get_product_userfields.side_effect = [
        {"voice_aliases": "sherry"},
        {"voice_aliases": "croft sherry\npale sherry"},
        {"voice_aliases": "croft sherry\npale sherry\nsherry"},
        {"voice_aliases": "croft sherry\npale sherry\nsherry"},
    ]
    client.async_get_products.return_value = [
        {"id": 97, "name": "sherry", "active": 1},
        {"id": 98, "name": "Croft sherry", "active": 1},
    ]
    resolver = AsyncMock()
    resolver.async_lookup_by_product_id.side_effect = [
        target,
        source,
        GrocyNotFoundError(),
        merged_old_name,
        merged,
        merged,
    ]
    aliases = AsyncMock()
    aliases.async_index.return_value = {
        "sherry": frozenset({97}),
        "croft sherry": frozenset({98}),
        "pale sherry": frozenset({98}),
    }
    journal = MemoryJournal()
    transactions = GrocyTransactionManager(client, resolver, journal)
    manager = GrocyProductMergeManager(
        client, resolver, transactions, aliases, journal
    )

    result = await manager.async_execute(
        product_id_to_keep=97,
        product_id_to_remove=98,
        canonical_name="Sherry",
        request_id="merge-sherry-2026-08-10",
        dry_run=False,
    )

    assert result["outcome"] == "committed"
    assert result["verification"]["verified"] is True
    client.async_merge_products.assert_awaited_once_with(97, 98)
    client.async_update_product.assert_awaited_once_with(97, {"name": "Sherry"})
    assert journal.records["merge-sherry-2026-08-10"]["result"]["success"] is True

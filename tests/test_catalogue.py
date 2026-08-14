"""Tests for confirmed and retry-safe product onboarding."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from custom_components.grocy_stock_manager.api import (
    GrocyMutationOutcomeUnknownError,
    GrocyNotFoundError,
)
from custom_components.grocy_stock_manager.catalogue import (
    CatalogueBarcodeConflictError,
    CatalogueLocationNotFoundError,
    CatalogueQuantityUnitNotFoundError,
    GrocyCatalogueManager,
)
from custom_components.grocy_stock_manager.models import (
    ProductDetails,
    ProductLookupResult,
    parse_stock_locations,
)

from .test_models import PRODUCT_DETAILS, STOCK_LOCATIONS


def _lookup(
    *,
    name: str = "Cat litter (Synthetic Grey)",
    barcode_amount: str = "1",
) -> ProductLookupResult:
    payload = {
        **PRODUCT_DETAILS,
        "product": {**PRODUCT_DETAILS["product"], "name": name},
        "product_barcodes": [
            {
                **PRODUCT_DETAILS["product_barcodes"][0],
                "amount": barcode_amount,
            }
        ],
    }
    product = ProductDetails.from_payload(payload)
    return ProductLookupResult(
        lookup_type="barcode",
        lookup_value="04260066669009",
        product=product,
        stock_locations=parse_stock_locations(STOCK_LOCATIONS),
        matched_barcode=product.barcodes[0],
    )


async def test_confirm_creates_product_barcode_and_verifies() -> None:
    """A new exact candidate creates minimal master data, then verifies by barcode."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = [GrocyNotFoundError, _lookup()]
    client.async_get_product_by_name.side_effect = GrocyNotFoundError
    client.async_get_locations.return_value = [
        {"id": "12", "name": "Garage Synthetic"}
    ]
    client.async_get_quantity_units.return_value = [{"id": "4", "name": "Pack"}]
    client.async_create_product.return_value = 1
    client.async_get_product_by_id.return_value = PRODUCT_DETAILS
    client.async_create_product_barcode.return_value = 10
    manager = GrocyCatalogueManager(client, resolver)

    result = await manager.async_confirm_product(
        barcode=" 04260066669009 ",
        product_name=" Cat litter (Synthetic Grey) ",
        location_id=None,
        location_name="Garage Synthetic",
        quantity_unit_id=None,
        quantity_unit_name=None,
    )

    assert result["catalogue_action"] == "created"
    assert result["product_created"] is True
    assert result["barcode_created"] is True
    client.async_create_product.assert_awaited_once_with(
        "Cat litter (Synthetic Grey)", location_id=12, quantity_unit_id=4
    )
    client.async_create_product_barcode.assert_awaited_once_with(
        1,
        "04260066669009",
        quantity_unit_id=4,
        amount=Decimal("1"),
    )


async def test_confirm_maps_to_exact_existing_product() -> None:
    """An exact name match gains the barcode without creating a duplicate product."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = [GrocyNotFoundError, _lookup()]
    client.async_get_product_by_name.return_value = PRODUCT_DETAILS
    client.async_create_product_barcode.return_value = 10
    manager = GrocyCatalogueManager(client, resolver)

    result = await manager.async_confirm_product(
        barcode="04260066669009",
        product_name="Cat litter (Synthetic Grey)",
        location_id=None,
        location_name="Garage Synthetic",
        quantity_unit_id=None,
        quantity_unit_name=None,
    )

    assert result["catalogue_action"] == "mapped"
    assert result["product_created"] is False
    client.async_create_product.assert_not_awaited()
    client.async_create_product_barcode.assert_awaited_once_with(
        1,
        "04260066669009",
        quantity_unit_id=4,
        amount=Decimal("1"),
    )


async def test_confirm_persists_multipack_amount_on_barcode() -> None:
    """One outer barcode can represent several product stock units."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = [
        GrocyNotFoundError,
        _lookup(barcode_amount="4"),
    ]
    client.async_get_product_by_name.return_value = PRODUCT_DETAILS
    client.async_create_product_barcode.return_value = 10
    manager = GrocyCatalogueManager(client, resolver)

    await manager.async_confirm_product(
        barcode="04260066669009",
        product_name="Cat litter (Synthetic Grey)",
        location_id=None,
        location_name="Garage Synthetic",
        quantity_unit_id=None,
        quantity_unit_name=None,
        barcode_amount=Decimal("4"),
    )

    client.async_create_product_barcode.assert_awaited_once_with(
        1,
        "04260066669009",
        quantity_unit_id=4,
        amount=Decimal("4"),
    )


async def test_confirm_retry_returns_existing_without_writes() -> None:
    """A repeated confirmation is idempotent once the barcode resolves."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.return_value = _lookup()
    manager = GrocyCatalogueManager(client, resolver)

    result = await manager.async_confirm_product(
        barcode="04260066669009",
        product_name="Cat litter (Synthetic Grey)",
        location_id=None,
        location_name="Garage Synthetic",
        quantity_unit_id=None,
        quantity_unit_name=None,
    )

    assert result["catalogue_action"] == "existing"
    client.async_create_product.assert_not_awaited()
    client.async_create_product_barcode.assert_not_awaited()


async def test_confirm_rejects_barcode_attached_to_different_product() -> None:
    """An existing barcode is never silently reassigned or accepted by guess."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.return_value = _lookup(name="Different product")
    manager = GrocyCatalogueManager(client, resolver)

    with pytest.raises(CatalogueBarcodeConflictError):
        await manager.async_confirm_product(
            barcode="04260066669009",
            product_name="Cat litter (Synthetic Grey)",
            location_id=None,
            location_name="Garage Synthetic",
            quantity_unit_id=None,
            quantity_unit_name=None,
        )


async def test_confirm_recovers_lost_barcode_create_response() -> None:
    """A lost POST response becomes success only after exact barcode verification."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = [GrocyNotFoundError, _lookup()]
    client.async_get_product_by_name.return_value = PRODUCT_DETAILS
    client.async_create_product_barcode.side_effect = GrocyMutationOutcomeUnknownError
    manager = GrocyCatalogueManager(client, resolver)

    result = await manager.async_confirm_product(
        barcode="04260066669009",
        product_name="Cat litter (Synthetic Grey)",
        location_id=None,
        location_name="Garage Synthetic",
        quantity_unit_id=None,
        quantity_unit_name=None,
    )

    assert result["catalogue_action"] == "mapped"
    assert result["barcode_created"] is True


async def test_confirm_recovers_lost_product_create_response() -> None:
    """A lost product POST response recovers only by exact name before mapping."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = [GrocyNotFoundError, _lookup()]
    client.async_get_product_by_name.side_effect = [
        GrocyNotFoundError,
        PRODUCT_DETAILS,
    ]
    client.async_get_locations.return_value = [
        {"id": "12", "name": "Garage Synthetic"}
    ]
    client.async_get_quantity_units.return_value = [{"id": "4", "name": "Pack"}]
    client.async_create_product.side_effect = GrocyMutationOutcomeUnknownError
    client.async_create_product_barcode.return_value = 10
    manager = GrocyCatalogueManager(client, resolver)

    result = await manager.async_confirm_product(
        barcode="04260066669009",
        product_name="Cat litter (Synthetic Grey)",
        location_id=None,
        location_name="Garage Synthetic",
        quantity_unit_id=None,
        quantity_unit_name=None,
    )

    assert result["catalogue_action"] == "created"
    assert result["product_created"] is True


async def test_confirm_marks_missing_post_write_barcode_as_unknown() -> None:
    """A successful POST without a verifiable barcode never reports success."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = [
        GrocyNotFoundError,
        GrocyNotFoundError,
    ]
    client.async_get_product_by_name.return_value = PRODUCT_DETAILS
    client.async_create_product_barcode.return_value = 10
    manager = GrocyCatalogueManager(client, resolver)

    with pytest.raises(GrocyMutationOutcomeUnknownError):
        await manager.async_confirm_product(
            barcode="04260066669009",
            product_name="Cat litter (Synthetic Grey)",
            location_id=None,
            location_name="Garage Synthetic",
            quantity_unit_id=None,
            quantity_unit_name=None,
        )


async def test_confirm_location_error_lists_available_names() -> None:
    """A bad shelf name reports Grocy's exact available location names."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = GrocyNotFoundError
    client.async_get_product_by_name.side_effect = GrocyNotFoundError
    client.async_get_locations.return_value = [
        {"id": "12", "name": "Garage Misc"},
        {"id": "8", "name": "Garage Left 1"},
        {"id": "invalid", "name": "Ignored"},
    ]
    manager = GrocyCatalogueManager(client, resolver)

    with pytest.raises(CatalogueLocationNotFoundError) as raised:
        await manager.async_confirm_product(
            barcode="079400152299",
            product_name="Synthetic product",
            location_id=None,
            location_name="R4",
            quantity_unit_id=None,
            quantity_unit_name=None,
        )

    assert raised.value.requested == "R4"
    assert raised.value.available_names == ("Garage Left 1", "Garage Misc")
    client.async_create_product.assert_not_awaited()


async def test_confirm_quantity_unit_error_lists_available_names() -> None:
    """A bad unit name reports Grocy's exact available quantity-unit names."""
    client = AsyncMock()
    resolver = AsyncMock()
    resolver.async_lookup_by_barcode.side_effect = GrocyNotFoundError
    client.async_get_product_by_name.side_effect = GrocyNotFoundError
    client.async_get_locations.return_value = [{"id": "12", "name": "Garage Misc"}]
    client.async_get_quantity_units.return_value = [
        {"id": "4", "name": "Pack"},
        {"id": "2", "name": "Each"},
    ]
    manager = GrocyCatalogueManager(client, resolver)

    with pytest.raises(CatalogueQuantityUnitNotFoundError) as raised:
        await manager.async_confirm_product(
            barcode="079400152299",
            product_name="Synthetic product",
            location_id=None,
            location_name="Garage Misc",
            quantity_unit_id=None,
            quantity_unit_name="Box",
        )

    assert raised.value.requested == "Box"
    assert raised.value.available_names == ("Each", "Pack")
    client.async_create_product.assert_not_awaited()

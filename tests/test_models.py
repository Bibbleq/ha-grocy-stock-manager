"""Tests for canonical Grocy response models."""

import pytest

from custom_components.grocy_stock_manager.api import GrocyInvalidResponseError
from custom_components.grocy_stock_manager.models import (
    ProductDetails,
    ProductLookupResult,
    parse_locations,
    parse_stock_entries,
    parse_stock_locations,
)

PRODUCT_DETAILS = {
    "product": {
        "id": "1",
        "name": "Cat litter (Synthetic Grey)",
        "default_consume_location_id": "12",
    },
    "product_barcodes": [
        {
            "product_id": "1",
            "barcode": "04260066669009",
            "qu_id": "4",
            "amount": "1",
        },
        {
            "product_id": "1",
            "barcode": "HOUSE-CAT-LITTER",
            "qu_id": None,
            "amount": None,
        },
    ],
    "quantity_unit_stock": {
        "id": "4",
        "name": "Pack",
        "name_plural": "Packs",
    },
    "stock_amount": "3.0",
    "default_location": {"id": "12", "name": "Garage Synthetic"},
}

STOCK_LOCATIONS = [
    {
        "product_id": "1",
        "amount": "1.0",
        "location_id": "13",
        "location_name": "Garage Z",
    },
    {
        "product_id": "1",
        "amount": "2.0",
        "location_id": "12",
        "location_name": "Garage A",
    },
]


def test_product_details_normalise_grocy_string_numbers() -> None:
    """Grocy's string-form ids and quantities become stable JSON types."""
    product = ProductDetails.from_payload(PRODUCT_DETAILS)

    assert product.id == 1
    assert product.stock_total == 3.0
    assert product.default_consume_location_id == 12
    assert product.default_location is not None
    assert product.default_location.id == 12
    assert product.quantity_unit.id == 4
    assert [item.barcode for item in product.barcodes] == [
        "04260066669009",
        "HOUSE-CAT-LITTER",
    ]
    assert product.barcodes[1].amount is None
    assert product.barcodes[1].quantity_unit_id is None


def test_lookup_response_is_canonical_and_deterministic() -> None:
    """The HA response is independent of Grocy ordering and raw types."""
    result = ProductLookupResult(
        lookup_type="barcode",
        lookup_value="04260066669009",
        product=ProductDetails.from_payload(PRODUCT_DETAILS),
        stock_locations=parse_stock_locations(STOCK_LOCATIONS),
    )

    response = result.as_service_response()
    assert response["success"] is True
    assert response["lookup_value"] == "04260066669009"
    assert response["product_id"] == 1
    assert response["product_name"] == "Cat litter (Synthetic Grey)"
    assert response["stock_total"] == 3.0
    assert response["default_location"] == {
        "id": 12,
        "name": "Garage Synthetic",
    }
    assert [item["location_name"] for item in response["stock_locations"]] == [
        "Garage A",
        "Garage Z",
    ]


def test_product_details_reject_invalid_ids() -> None:
    """Malformed Grocy data is rejected rather than guessed."""
    payload = {**PRODUCT_DETAILS, "product": {"id": "not-an-id", "name": "Bad"}}

    with pytest.raises(GrocyInvalidResponseError):
        ProductDetails.from_payload(payload)


def test_location_and_stock_entry_reads_are_normalised() -> None:
    """Generic locations and stock entries also hide Grocy string numbers."""
    locations = parse_locations([{"id": "12", "name": "Garage Synthetic"}])
    entries = parse_stock_entries(
        [
            {
                "id": "4",
                "stock_id": "synthetic-stock-id",
                "product_id": "1",
                "location_id": "12",
                "amount": "3.0",
            }
        ]
    )

    assert locations[0].id == 12
    assert entries[0].id == 4
    assert entries[0].location_id == 12
    assert entries[0].amount == 3.0

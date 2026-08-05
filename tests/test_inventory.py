"""Tests for the location-aware inventory snapshot."""

from custom_components.grocy_stock_manager.inventory import GrocyInventory


class FakeInventoryClient:
    """Return deterministic Grocy catalogue and stock payloads."""

    async def async_get_products(self):
        return [
            {"id": "2", "name": "Hair gel", "qu_id_stock": "1"},
            {"id": "1", "name": "Cat litter", "qu_id_stock": "1"},
            {"id": "3", "name": "Empty product", "qu_id_stock": "1"},
        ]

    async def async_get_locations(self):
        return [
            {"id": "12", "name": "Garage R1"},
            {"id": "11", "name": "Garage L1"},
            {"id": "13", "name": "Garage Misc"},
        ]

    async def async_get_quantity_units(self):
        return [{"id": "1", "name": "Pack", "name_plural": "Packs"}]

    async def async_get_product_stock_locations(self, product_id: int):
        return {
            1: [
                {
                    "location_id": "12",
                    "location_name": "Garage R1",
                    "amount": "1",
                },
                {
                    "location_id": "11",
                    "location_name": "Garage L1",
                    "amount": "2",
                },
            ],
            2: [
                {
                    "location_id": "11",
                    "location_name": "Garage L1",
                    "amount": "0.5",
                }
            ],
            3: [],
        }[product_id]


async def test_snapshot_groups_stock_by_product_and_location() -> None:
    """The snapshot preserves multi-location stock and empty shelves."""
    snapshot = await GrocyInventory(FakeInventoryClient()).async_snapshot()

    assert snapshot.stocked_product_count == 2
    assert snapshot.occupied_location_count == 2
    assert [product.product_name for product in snapshot.products] == [
        "Cat litter",
        "Hair gel",
    ]
    assert snapshot.products[0].stock_total == 3

    attributes = snapshot.as_attributes()
    assert attributes["products"][0] == {
        "product_id": 1,
        "product_name": "Cat litter",
        "quantity_unit": "Pack",
        "stock_total": 3,
        "locations": [
            {
                "location_id": 11,
                "location_name": "Garage L1",
                "amount": 2,
            },
            {
                "location_id": 12,
                "location_name": "Garage R1",
                "amount": 1,
            },
        ],
    }
    assert [location["location_name"] for location in attributes["locations"]] == [
        "Garage L1",
        "Garage Misc",
        "Garage R1",
    ]
    assert attributes["locations"][1] == {
        "location_id": 13,
        "location_name": "Garage Misc",
        "product_count": 0,
        "products": [],
    }
    assert attributes["locations"][0]["products"][1]["amount"] == 0.5

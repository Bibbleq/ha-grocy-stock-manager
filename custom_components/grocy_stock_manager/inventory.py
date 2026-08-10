"""Read-only, location-aware Grocy inventory snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .api import GrocyApiClient, GrocyInvalidResponseError, GrocyNotFoundError
from .models import parse_stock_locations
from .voice import normalise_product_phrase, parse_alias_value

_MAX_CONCURRENT_REQUESTS = 8


def _integer(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise GrocyInvalidResponseError(
            f"Grocy field {field!r} is not an integer"
        ) from err


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GrocyInvalidResponseError(f"Grocy field {field!r} is not a string")
    return value.strip()


def _response_number(value: Decimal) -> int | float:
    """Return clean JSON-safe quantities without unnecessary .0 suffixes."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


@dataclass(frozen=True, slots=True)
class InventoryProductLocation:
    """One product quantity at one Grocy location."""

    location_id: int
    location_name: str
    amount: Decimal

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant state-attribute-safe representation."""
        return {
            "location_id": self.location_id,
            "location_name": self.location_name,
            "amount": _response_number(self.amount),
        }


@dataclass(frozen=True, slots=True)
class InventoryProduct:
    """Current stock for one Grocy product."""

    product_id: int
    product_name: str
    quantity_unit: str
    barcodes: tuple[str, ...]
    voice_aliases: tuple[str, ...]
    stock_total: Decimal
    locations: tuple[InventoryProductLocation, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant state-attribute-safe representation."""
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "quantity_unit": self.quantity_unit,
            "barcodes": list(self.barcodes),
            "voice_aliases": list(self.voice_aliases),
            "search_text": " ".join(
                (
                    normalise_product_phrase(self.product_name),
                    *self.voice_aliases,
                    *self.barcodes,
                )
            ),
            "stock_total": _response_number(self.stock_total),
            "locations": [location.as_dict() for location in self.locations],
        }


@dataclass(frozen=True, slots=True)
class InventoryLocationProduct:
    """One product shown in a shelf breakdown."""

    product_id: int
    product_name: str
    quantity_unit: str
    barcodes: tuple[str, ...]
    voice_aliases: tuple[str, ...]
    amount: Decimal

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant state-attribute-safe representation."""
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "quantity_unit": self.quantity_unit,
            "barcodes": list(self.barcodes),
            "voice_aliases": list(self.voice_aliases),
            "search_text": " ".join(
                (
                    normalise_product_phrase(self.product_name),
                    *self.voice_aliases,
                    *self.barcodes,
                )
            ),
            "amount": _response_number(self.amount),
        }


@dataclass(frozen=True, slots=True)
class InventoryLocation:
    """A Grocy location and the stock currently stored there."""

    location_id: int
    location_name: str
    products: tuple[InventoryLocationProduct, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant state-attribute-safe representation."""
        return {
            "location_id": self.location_id,
            "location_name": self.location_name,
            "product_count": len(self.products),
            "products": [product.as_dict() for product in self.products],
        }


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """One internally consistent, read-only view of Grocy stock."""

    products: tuple[InventoryProduct, ...]
    locations: tuple[InventoryLocation, ...]

    @property
    def stocked_product_count(self) -> int:
        """Return the number of distinct products with positive stock."""
        return len(self.products)

    @property
    def occupied_location_count(self) -> int:
        """Return the number of locations containing positive stock."""
        return sum(bool(location.products) for location in self.locations)

    def as_attributes(self) -> dict[str, Any]:
        """Return stable sensor attributes for dashboards and automations."""
        return {
            "stocked_product_count": self.stocked_product_count,
            "occupied_location_count": self.occupied_location_count,
            "products": [product.as_dict() for product in self.products],
            "locations": [location.as_dict() for location in self.locations],
        }


class GrocyInventory:
    """Build a complete stock snapshot from authoritative Grocy data."""

    def __init__(self, client: GrocyApiClient) -> None:
        """Initialise the inventory reader."""
        self._client = client

    async def async_snapshot(self) -> InventorySnapshot:
        """Return current positive stock grouped by product and location."""
        (
            raw_products,
            raw_locations,
            raw_quantity_units,
            raw_barcodes,
        ) = await asyncio.gather(
            self._client.async_get_products(),
            self._client.async_get_locations(),
            self._client.async_get_quantity_units(),
            self._client.async_get_product_barcodes(),
        )

        quantity_units = {
            _integer(item.get("id"), "quantity_units[].id"): _string(
                item.get("name"), "quantity_units[].name"
            )
            for item in raw_quantity_units
        }
        barcodes_by_product: dict[int, list[str]] = {}
        for item in raw_barcodes:
            product_id = _integer(
                item.get("product_id"), "product_barcodes[].product_id"
            )
            barcode = _string(item.get("barcode"), "product_barcodes[].barcode")
            barcodes_by_product.setdefault(product_id, []).append(barcode)

        product_summaries = [
            (
                _integer(item.get("id"), "products[].id"),
                _string(item.get("name"), "products[].name"),
                quantity_units.get(
                    _integer(item.get("qu_id_stock"), "products[].qu_id_stock"),
                    "unit",
                ),
                tuple(
                    sorted(
                        barcodes_by_product.get(
                            _integer(item.get("id"), "products[].id"), []
                        )
                    )
                ),
                parse_alias_value(
                    item.get("userfields", {}).get("voice_aliases")
                    if isinstance(item.get("userfields"), Mapping)
                    else None
                ),
            )
            for item in raw_products
        ]

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)

        async def async_product_stock(
            product_id: int,
            product_name: str,
            quantity_unit: str,
            barcodes: tuple[str, ...],
            voice_aliases: tuple[str, ...],
        ) -> InventoryProduct | None:
            async with semaphore:
                try:
                    raw_stock_locations = (
                        await self._client.async_get_product_stock_locations(product_id)
                    )
                except GrocyNotFoundError:
                    return None

            locations = tuple(
                InventoryProductLocation(
                    location_id=location.location_id,
                    location_name=location.location_name,
                    amount=location.amount,
                )
                for location in parse_stock_locations(raw_stock_locations)
                if location.amount > 0
            )
            if not locations:
                return None
            return InventoryProduct(
                product_id=product_id,
                product_name=product_name,
                quantity_unit=quantity_unit,
                barcodes=barcodes,
                voice_aliases=voice_aliases,
                stock_total=sum(
                    (location.amount for location in locations), Decimal("0")
                ),
                locations=locations,
            )

        fetched_products = await asyncio.gather(
            *(
                async_product_stock(
                    product_id,
                    product_name,
                    quantity_unit,
                    barcodes,
                    voice_aliases,
                )
                for (
                    product_id,
                    product_name,
                    quantity_unit,
                    barcodes,
                    voice_aliases,
                ) in product_summaries
            )
        )
        products = tuple(
            sorted(
                (product for product in fetched_products if product is not None),
                key=lambda product: (
                    product.product_name.casefold(),
                    product.product_id,
                ),
            )
        )

        products_by_location: dict[int, list[InventoryLocationProduct]] = {}
        for product in products:
            for location in product.locations:
                products_by_location.setdefault(location.location_id, []).append(
                    InventoryLocationProduct(
                        product_id=product.product_id,
                        product_name=product.product_name,
                        quantity_unit=product.quantity_unit,
                        barcodes=product.barcodes,
                        voice_aliases=product.voice_aliases,
                        amount=location.amount,
                    )
                )

        configured_locations = {
            _integer(item.get("id"), "locations[].id"): _string(
                item.get("name"), "locations[].name"
            )
            for item in raw_locations
        }
        for product in products:
            for location in product.locations:
                configured_locations.setdefault(
                    location.location_id, location.location_name
                )

        locations = tuple(
            InventoryLocation(
                location_id=location_id,
                location_name=location_name,
                products=tuple(
                    sorted(
                        products_by_location.get(location_id, []),
                        key=lambda product: (
                            product.product_name.casefold(),
                            product.product_id,
                        ),
                    )
                ),
            )
            for location_id, location_name in sorted(
                configured_locations.items(),
                key=lambda item: (item[1].casefold(), item[0]),
            )
        )
        return InventorySnapshot(products=products, locations=locations)

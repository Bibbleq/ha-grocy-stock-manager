"""Canonical read-only models for Grocy stock data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .api import GrocyInvalidResponseError


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GrocyInvalidResponseError(f"Grocy field {field!r} is not an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise GrocyInvalidResponseError(f"Grocy field {field!r} is not an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GrocyInvalidResponseError(f"Grocy field {field!r} is not a string")
    return value


def _integer(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise GrocyInvalidResponseError(
            f"Grocy field {field!r} is not an integer"
        ) from err


def _optional_integer(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    return _integer(value, field)


def _number(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as err:
        raise GrocyInvalidResponseError(
            f"Grocy field {field!r} is not a number"
        ) from err
    if not number.is_finite():
        raise GrocyInvalidResponseError(
            f"Grocy field {field!r} is not a finite number"
        )
    return number


def _optional_number(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _number(value, field)


def _response_number(value: Decimal | None) -> float | None:
    """Convert an exact internal quantity at the Home Assistant boundary."""
    return float(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class QuantityUnit:
    """A Grocy quantity unit used for stock amounts."""

    id: int
    name: str
    name_plural: str | None

    @classmethod
    def from_payload(cls, payload: Any) -> QuantityUnit:
        """Build a quantity unit from Grocy JSON."""
        data = _mapping(payload, "quantity_unit_stock")
        name_plural = data.get("name_plural")
        if name_plural is not None and not isinstance(name_plural, str):
            raise GrocyInvalidResponseError(
                "Grocy field 'quantity_unit_stock.name_plural' is not a string"
            )
        return cls(
            id=_integer(data.get("id"), "quantity_unit_stock.id"),
            name=_string(data.get("name"), "quantity_unit_stock.name"),
            name_plural=name_plural,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant response-safe representation."""
        return {
            "id": self.id,
            "name": self.name,
            "name_plural": self.name_plural,
        }


@dataclass(frozen=True, slots=True)
class Location:
    """A Grocy stock location."""

    id: int
    name: str

    @classmethod
    def from_payload(cls, payload: Any, field: str = "location") -> Location:
        """Build a location from Grocy JSON."""
        data = _mapping(payload, field)
        return cls(
            id=_integer(data.get("id"), f"{field}.id"),
            name=_string(data.get("name"), f"{field}.name"),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant response-safe representation."""
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True, slots=True)
class ProductBarcode:
    """One barcode associated with a Grocy product."""

    barcode: str
    amount: Decimal | None
    quantity_unit_id: int | None

    @classmethod
    def from_payload(cls, payload: Any) -> ProductBarcode:
        """Build a product barcode from Grocy JSON."""
        data = _mapping(payload, "product_barcodes[]")
        return cls(
            barcode=_string(data.get("barcode"), "product_barcodes[].barcode"),
            amount=_optional_number(
                data.get("amount"), "product_barcodes[].amount"
            ),
            quantity_unit_id=_optional_integer(
                data.get("qu_id"), "product_barcodes[].qu_id"
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant response-safe representation."""
        return {
            "barcode": self.barcode,
            "amount": _response_number(self.amount),
            "quantity_unit_id": self.quantity_unit_id,
        }


@dataclass(frozen=True, slots=True)
class StockLocation:
    """Current stock for a product at one Grocy location."""

    location_id: int
    location_name: str
    amount: Decimal

    @classmethod
    def from_payload(cls, payload: Any) -> StockLocation:
        """Build a stock location from Grocy JSON."""
        data = _mapping(payload, "stock_locations[]")
        return cls(
            location_id=_integer(
                data.get("location_id"), "stock_locations[].location_id"
            ),
            location_name=_string(
                data.get("location_name"), "stock_locations[].location_name"
            ),
            amount=_number(data.get("amount"), "stock_locations[].amount"),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a Home Assistant response-safe representation."""
        return {
            "location_id": self.location_id,
            "location_name": self.location_name,
            "amount": _response_number(self.amount),
        }


@dataclass(frozen=True, slots=True)
class StockEntry:
    """A single Grocy stock entry."""

    id: int
    stock_id: str
    product_id: int
    location_id: int
    amount: Decimal

    @classmethod
    def from_payload(cls, payload: Any) -> StockEntry:
        """Build a stock entry from Grocy JSON."""
        data = _mapping(payload, "stock_entries[]")
        return cls(
            id=_integer(data.get("id"), "stock_entries[].id"),
            stock_id=_string(data.get("stock_id"), "stock_entries[].stock_id"),
            product_id=_integer(
                data.get("product_id"), "stock_entries[].product_id"
            ),
            location_id=_integer(
                data.get("location_id"), "stock_entries[].location_id"
            ),
            amount=_number(data.get("amount"), "stock_entries[].amount"),
        )


@dataclass(frozen=True, slots=True)
class ProductDetails:
    """Canonical product details returned by Grocy."""

    id: int
    name: str
    barcodes: tuple[ProductBarcode, ...]
    quantity_unit: QuantityUnit
    stock_total: Decimal
    default_location: Location | None
    default_consume_location_id: int | None

    @classmethod
    def from_payload(cls, payload: Any) -> ProductDetails:
        """Build product details from Grocy JSON."""
        data = _mapping(payload, "product_details")
        product = _mapping(data.get("product"), "product")
        raw_barcodes = _sequence(data.get("product_barcodes", []), "product_barcodes")

        raw_default_location = data.get("default_location")
        if raw_default_location is None:
            raw_default_location = data.get("location")

        return cls(
            id=_integer(product.get("id"), "product.id"),
            name=_string(product.get("name"), "product.name"),
            barcodes=tuple(ProductBarcode.from_payload(item) for item in raw_barcodes),
            quantity_unit=QuantityUnit.from_payload(data.get("quantity_unit_stock")),
            stock_total=_number(data.get("stock_amount", 0), "stock_amount"),
            default_location=(
                Location.from_payload(raw_default_location, "default_location")
                if raw_default_location is not None
                else None
            ),
            default_consume_location_id=_optional_integer(
                product.get("default_consume_location_id"),
                "product.default_consume_location_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class ProductLookupResult:
    """Stable response contract shared by HA callers."""

    lookup_type: str
    lookup_value: str | int
    product: ProductDetails
    stock_locations: tuple[StockLocation, ...]
    matched_barcode: ProductBarcode | None = None

    def as_service_response(self) -> dict[str, Any]:
        """Return the structured Home Assistant action response."""
        default_location = self.product.default_location
        return {
            "response_version": 1,
            "success": True,
            "lookup_type": self.lookup_type,
            "lookup_value": self.lookup_value,
            "product_id": self.product.id,
            "product_name": self.product.name,
            "barcodes": [barcode.as_dict() for barcode in self.product.barcodes],
            "matched_barcode": (
                self.matched_barcode.as_dict()
                if self.matched_barcode is not None
                else None
            ),
            "quantity_unit": self.product.quantity_unit.as_dict(),
            "stock_total": _response_number(self.product.stock_total),
            "stock_locations": [
                location.as_dict() for location in self.stock_locations
            ],
            "default_location": (
                default_location.as_dict() if default_location is not None else None
            ),
            "default_consume_location_id": (
                self.product.default_consume_location_id
            ),
        }


def parse_stock_locations(payload: Any) -> tuple[StockLocation, ...]:
    """Parse and deterministically order Grocy stock-location data."""
    locations = [
        StockLocation.from_payload(item)
        for item in _sequence(payload, "stock_locations")
    ]
    return tuple(
        sorted(locations, key=lambda item: (item.location_name, item.location_id))
    )


def parse_stock_entries(payload: Any) -> tuple[StockEntry, ...]:
    """Parse Grocy stock-entry data."""
    return tuple(
        StockEntry.from_payload(item) for item in _sequence(payload, "stock_entries")
    )


def parse_locations(payload: Any) -> tuple[Location, ...]:
    """Parse and deterministically order Grocy locations."""
    locations = [
        Location.from_payload(item, "locations[]")
        for item in _sequence(payload, "locations")
    ]
    return tuple(sorted(locations, key=lambda item: (item.name, item.id)))


def parse_product_summary_id(payload: Any) -> int:
    """Return the product id from a generic Grocy product object."""
    data = _mapping(payload, "products[]")
    return _integer(data.get("id"), "products[].id")

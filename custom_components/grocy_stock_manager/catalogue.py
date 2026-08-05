"""Confirmed, retry-safe Grocy product and barcode onboarding."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from .api import (
    GrocyApiClient,
    GrocyApiError,
    GrocyMutationOutcomeUnknownError,
    GrocyNotFoundError,
)
from .models import ProductDetails, ProductLookupResult
from .resolver import GrocyProductResolver


class CatalogueError(Exception):
    """Base error for confirmed catalogue changes."""


class CatalogueLocationNotFoundError(CatalogueError):
    """Raised when the requested default location is not exact and unique."""

    def __init__(self, requested: str, available_names: Sequence[str]) -> None:
        self.requested = requested
        self.available_names = tuple(available_names)
        super().__init__(requested)


class CatalogueQuantityUnitNotFoundError(CatalogueError):
    """Raised when the requested quantity unit is not exact and unique."""

    def __init__(self, requested: str, available_names: Sequence[str]) -> None:
        self.requested = requested
        self.available_names = tuple(available_names)
        super().__init__(requested)


class CatalogueBarcodeConflictError(CatalogueError):
    """Raised when a barcode already belongs to a different product."""


def _object_id(payload: Mapping[str, Any]) -> int | None:
    try:
        value = int(payload.get("id"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _resolve_object(
    payload: Sequence[Mapping[str, Any]],
    *,
    object_id: int | None,
    object_name: str | None,
) -> tuple[int, str] | None:
    """Resolve one object by exact ID or case-insensitive exact name."""
    matches: list[tuple[int, str]] = []
    for item in payload:
        candidate_id = _object_id(item)
        candidate_name = item.get("name")
        if candidate_id is None or not isinstance(candidate_name, str):
            continue
        if (
            object_id is not None and candidate_id == object_id
        ) or (
            object_name is not None
            and candidate_name.casefold() == object_name.casefold()
        ):
            matches.append((candidate_id, candidate_name))
    if len(matches) != 1:
        return None
    return matches[0]


def _available_names(payload: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Return valid object names in a stable, human-readable order."""
    return tuple(
        sorted(
            {
                name.strip()
                for item in payload
                if _object_id(item) is not None
                and isinstance((name := item.get("name")), str)
                and name.strip()
            },
            key=str.casefold,
        )
    )


def _requested_object(object_id: int | None, object_name: str | None) -> str:
    """Describe the object selector supplied by the caller."""
    if object_id is not None:
        return f"ID {object_id}"
    return object_name or "(none)"


class GrocyCatalogueManager:
    """Create or map a product only after an explicit confirmation action."""

    def __init__(
        self,
        client: GrocyApiClient,
        resolver: GrocyProductResolver,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._lock = asyncio.Lock()

    @staticmethod
    def _assert_same_product(
        lookup: ProductLookupResult,
        product_name: str,
    ) -> None:
        if lookup.product.name.casefold() != product_name.casefold():
            raise CatalogueBarcodeConflictError

    @staticmethod
    def _response(
        lookup: ProductLookupResult,
        *,
        action: str,
        product_created: bool,
        barcode_created: bool,
    ) -> dict[str, Any]:
        return {
            "response_version": 1,
            "success": True,
            "catalogue_action": action,
            "product_created": product_created,
            "barcode_created": barcode_created,
            "product_id": lookup.product.id,
            "product_name": lookup.product.name,
            "barcode": str(lookup.lookup_value),
            "quantity_unit": lookup.product.quantity_unit.as_dict(),
            "default_location": (
                lookup.product.default_location.as_dict()
                if lookup.product.default_location is not None
                else None
            ),
        }

    async def _async_existing_barcode(
        self,
        barcode: str,
        product_name: str,
    ) -> ProductLookupResult | None:
        try:
            lookup = await self._resolver.async_lookup_by_barcode(barcode)
        except GrocyNotFoundError:
            return None
        self._assert_same_product(lookup, product_name)
        return lookup

    async def _async_resolve_location(
        self,
        location_id: int | None,
        location_name: str | None,
    ) -> tuple[int, str]:
        locations = await self._client.async_get_locations()
        match = _resolve_object(
            locations,
            object_id=location_id,
            object_name=location_name,
        )
        if match is None:
            raise CatalogueLocationNotFoundError(
                _requested_object(location_id, location_name),
                _available_names(locations),
            )
        return match

    async def _async_resolve_quantity_unit(
        self,
        quantity_unit_id: int | None,
        quantity_unit_name: str | None,
    ) -> tuple[int, str]:
        quantity_units = await self._client.async_get_quantity_units()
        requested_name = quantity_unit_name or "Pack"
        match = _resolve_object(
            quantity_units,
            object_id=quantity_unit_id,
            object_name=requested_name,
        )
        if match is None:
            raise CatalogueQuantityUnitNotFoundError(
                _requested_object(quantity_unit_id, requested_name),
                _available_names(quantity_units),
            )
        return match

    async def async_confirm_product(
        self,
        *,
        barcode: str,
        product_name: str,
        location_id: int | None,
        location_name: str | None,
        quantity_unit_id: int | None,
        quantity_unit_name: str | None,
    ) -> dict[str, Any]:
        """Idempotently create/map a confirmed barcode and verify the result."""
        barcode = barcode.strip()
        product_name = product_name.strip()

        async with self._lock:
            existing = await self._async_existing_barcode(barcode, product_name)
            if existing is not None:
                return self._response(
                    existing,
                    action="existing",
                    product_created=False,
                    barcode_created=False,
                )

            product_created = False
            try:
                details = await self._client.async_get_product_by_name(product_name)
                product = ProductDetails.from_payload(details)
            except GrocyNotFoundError:
                resolved_location = await self._async_resolve_location(
                    location_id,
                    location_name,
                )
                resolved_qu = await self._async_resolve_quantity_unit(
                    quantity_unit_id,
                    quantity_unit_name,
                )
                try:
                    product_id = await self._client.async_create_product(
                        product_name,
                        location_id=resolved_location[0],
                        quantity_unit_id=resolved_qu[0],
                    )
                except GrocyMutationOutcomeUnknownError as err:
                    # The POST may have committed. Recover only through an exact,
                    # unique name read; otherwise preserve the unknown outcome.
                    try:
                        details = await self._client.async_get_product_by_name(
                            product_name
                        )
                    except GrocyNotFoundError as recovery_err:
                        raise err from recovery_err
                    product = ProductDetails.from_payload(details)
                    product_created = True
                else:
                    try:
                        details = await self._client.async_get_product_by_id(
                            product_id
                        )
                    except GrocyApiError as err:
                        raise GrocyMutationOutcomeUnknownError from err
                    product = ProductDetails.from_payload(details)
                    product_created = True

            try:
                await self._client.async_create_product_barcode(
                    product.id,
                    barcode,
                    quantity_unit_id=product.quantity_unit.id,
                )
            except GrocyMutationOutcomeUnknownError:
                # Verification converts a lost response into a known success only
                # when the barcode now resolves to the intended exact product.
                recovered = await self._async_existing_barcode(barcode, product_name)
                if recovered is None:
                    raise
                return self._response(
                    recovered,
                    action="created" if product_created else "mapped",
                    product_created=product_created,
                    barcode_created=True,
                )

            try:
                verified = await self._resolver.async_lookup_by_barcode(barcode)
            except GrocyNotFoundError as err:
                raise GrocyMutationOutcomeUnknownError from err
            if verified.product.id != product.id:
                raise CatalogueBarcodeConflictError
            self._assert_same_product(verified, product_name)
            return self._response(
                verified,
                action="created" if product_created else "mapped",
                product_created=product_created,
                barcode_created=True,
            )

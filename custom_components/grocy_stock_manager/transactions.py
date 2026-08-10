"""Verified, idempotent Grocy stock transaction engine."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Literal

from .api import GrocyApiClient, GrocyApiError, GrocyMutationOutcomeUnknownError
from .journal import TransactionJournal
from .models import Location, ProductLookupResult, parse_locations
from .resolver import GrocyProductResolver

Operation = Literal["add", "consume"]


class TransactionValidationError(Exception):
    """Base class for safe transaction rejections."""


class TransactionLocationNotFoundError(TransactionValidationError):
    """Raised when an explicit or required location cannot be resolved."""


class TransactionLocationAmbiguousError(TransactionValidationError):
    """Raised when consumption cannot safely infer one location."""


class TransactionInsufficientStockError(TransactionValidationError):
    """Raised when a location does not contain the requested amount."""


class TransactionRequestConflictError(TransactionValidationError):
    """Raised when a request ID is reused for different work."""


def _decimal_response(value: Decimal) -> float:
    return float(value)


class GrocyTransactionManager:
    """Make one Grocy mutation at a time per product and verify its effect."""

    def __init__(
        self,
        client: GrocyApiClient,
        resolver: GrocyProductResolver,
        journal: TransactionJournal,
    ) -> None:
        """Initialise the transaction manager."""
        self._client = client
        self._resolver = resolver
        self._journal = journal
        self._product_locks: defaultdict[int, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )
        self._request_locks: defaultdict[str, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    @asynccontextmanager
    async def async_lock_products(self, *product_ids: int):
        """Prevent stock work on a deterministic set of products."""
        locks = [self._product_locks[item] for item in sorted(set(product_ids))]
        for lock in locks:
            await lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    async def async_execute(
        self,
        operation: Operation,
        lookup: ProductLookupResult,
        *,
        amount: Decimal,
        request_id: str,
        location_id: int | None,
        location_name: str | None,
        source: str,
    ) -> dict[str, Any]:
        """Execute or replay one idempotent, verified transaction."""
        fingerprint = json.dumps(
            {
                "operation": operation,
                "product_id": lookup.product.id,
                "amount": format(amount, "f"),
                "location_id": location_id,
                "location_name": location_name,
                "source": source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        async with self._request_locks[request_id]:
            prior = await self._journal.async_get(request_id)
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise TransactionRequestConflictError
                replay = dict(prior["result"])
                replay["replayed"] = True
                return replay

            async with self._product_locks[lookup.product.id]:
                # The identifier lookup may have waited behind another transaction.
                # Refresh under the product lock so the pre-write baseline is current.
                lookup = await self._resolver.async_lookup_by_product_id(
                    lookup.product.id
                )
                location = await self._async_select_location(
                    operation,
                    lookup,
                    amount=amount,
                    location_id=location_id,
                    location_name=location_name,
                )
                before = self._location_amount(lookup, location.id)
                expected_after = (
                    before + amount if operation == "add" else before - amount
                )

                # Persist a fail-safe claim before the POST. If HA stops between
                # sending and recording the final result, replaying this request
                # returns "unknown" instead of risking a duplicate mutation.
                pending_result: dict[str, Any] = {
                    "response_version": 1,
                    "success": False,
                    "outcome": "unknown",
                    "replayed": False,
                    "operation": operation,
                    "request_id": request_id,
                    "source": source,
                    "product_id": lookup.product.id,
                    "product_name": lookup.product.name,
                    "location_id": location.id,
                    "location_name": location.name,
                    "quantity_unit": lookup.product.quantity_unit.as_dict(),
                    "amount": _decimal_response(amount),
                    "stock_before": _decimal_response(before),
                    "stock_after": None,
                    "expected_stock_after": _decimal_response(expected_after),
                    "transaction_count": None,
                    "requires_reconciliation": True,
                    "uncertainty_reason": "in_progress_or_interrupted",
                }
                await self._journal.async_record(
                    request_id, fingerprint, pending_result
                )

                transaction_count: int | None = None
                transport_uncertain = False
                try:
                    if operation == "add":
                        transactions = await self._client.async_add_product(
                            lookup.product.id,
                            amount=format(amount, "f"),
                            location_id=location.id,
                        )
                    else:
                        transactions = await self._client.async_consume_product(
                            lookup.product.id,
                            amount=format(amount, "f"),
                            location_id=location.id,
                        )
                    transaction_count = len(transactions)
                except GrocyMutationOutcomeUnknownError:
                    transport_uncertain = True

                after_lookup: ProductLookupResult | None = None
                try:
                    after_lookup = await self._resolver.async_lookup_by_product_id(
                        lookup.product.id
                    )
                except GrocyApiError:
                    # The POST completed or was uncertain, but the verification
                    # read also failed. The durable pending claim prevents retry.
                    transport_uncertain = True

                after = (
                    self._location_amount(after_lookup, location.id)
                    if after_lookup is not None
                    else None
                )
                verified = after == expected_after
                outcome = "committed" if verified else "unknown"
                result = {
                    **pending_result,
                    "success": verified,
                    "outcome": outcome,
                    "stock_after": (
                        _decimal_response(after) if after is not None else None
                    ),
                    "transaction_count": transaction_count,
                    "requires_reconciliation": not verified,
                    "uncertainty_reason": (
                        None
                        if verified
                        else (
                            "transport_or_response_unknown"
                            if transport_uncertain
                            else "verification_mismatch"
                        )
                    ),
                }
                await self._journal.async_record(request_id, fingerprint, result)
                return result

    async def _async_select_location(
        self,
        operation: Operation,
        lookup: ProductLookupResult,
        *,
        amount: Decimal,
        location_id: int | None,
        location_name: str | None,
    ) -> Location:
        """Resolve an explicit location or apply fail-closed inference rules."""
        if location_id is not None or location_name is not None:
            locations = parse_locations(await self._client.async_get_locations())
            if location_id is not None:
                match = next(
                    (item for item in locations if item.id == location_id), None
                )
            else:
                matches = [
                    item
                    for item in locations
                    if item.name.casefold() == location_name.casefold()
                ]
                if len(matches) > 1:
                    raise TransactionLocationAmbiguousError
                match = matches[0] if matches else None
            if match is None:
                raise TransactionLocationNotFoundError
            self._validate_consume_stock(operation, lookup, match.id, amount)
            return match

        if operation == "add":
            if lookup.product.default_location is None:
                raise TransactionLocationNotFoundError
            return lookup.product.default_location

        stocked = [item for item in lookup.stock_locations if item.amount > 0]
        if len(stocked) == 1:
            item = stocked[0]
            if item.amount < amount:
                raise TransactionInsufficientStockError
            return Location(item.location_id, item.location_name)

        default_id = lookup.product.default_consume_location_id
        default = next(
            (item for item in stocked if item.location_id == default_id), None
        )
        if default is not None and default.amount >= amount:
            return Location(default.location_id, default.location_name)

        sufficient = [item for item in stocked if item.amount >= amount]
        if len(sufficient) == 1:
            item = sufficient[0]
            return Location(item.location_id, item.location_name)
        if not sufficient:
            raise TransactionInsufficientStockError
        raise TransactionLocationAmbiguousError

    @staticmethod
    def _validate_consume_stock(
        operation: Operation,
        lookup: ProductLookupResult,
        location_id: int,
        amount: Decimal,
    ) -> None:
        if (
            operation == "consume"
            and GrocyTransactionManager._location_amount(lookup, location_id) < amount
        ):
            raise TransactionInsufficientStockError

    @staticmethod
    def _location_amount(
        lookup: ProductLookupResult | None, location_id: int
    ) -> Decimal:
        if lookup is None:
            return Decimal(0)
        location = next(
            (
                item
                for item in lookup.stock_locations
                if item.location_id == location_id
            ),
            None,
        )
        return location.amount if location is not None else Decimal(0)

"""Guarded product consolidation using Grocy's native atomic merge."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from decimal import Decimal
from typing import Any

from .api import (
    GrocyApiClient,
    GrocyApiError,
    GrocyMutationOutcomeUnknownError,
    GrocyNotFoundError,
)
from .const import VOICE_ALIAS_USERFIELD
from .journal import TransactionJournal
from .models import ProductLookupResult
from .resolver import GrocyProductResolver
from .transactions import GrocyTransactionManager, TransactionRequestConflictError
from .voice import GrocyVoiceAliases, _serialise_aliases, parse_alias_value


class MergeValidationError(Exception):
    """Base class for a safe product-merge rejection."""


class MergeSameProductError(MergeValidationError):
    """Raised when the keep and remove IDs are identical."""


class MergeQuantityUnitMismatchError(MergeValidationError):
    """Raised when products use different stock quantity units."""


class MergeAliasConflictError(MergeValidationError):
    """Raised when an alias also belongs to an unrelated product."""


class MergeNameConflictError(MergeValidationError):
    """Raised when the requested canonical name belongs to another product."""


def _number(value: Decimal) -> float:
    return float(value)


def _stock_by_location(lookup: ProductLookupResult) -> dict[int, dict[str, Any]]:
    return {
        item.location_id: {
            "location_id": item.location_id,
            "location_name": item.location_name,
            "amount": item.amount,
        }
        for item in lookup.stock_locations
        if item.amount != 0
    }


def _expected_stock(
    target: ProductLookupResult, source: ProductLookupResult
) -> list[dict[str, Any]]:
    combined: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"location_id": 0, "location_name": "", "amount": Decimal(0)}
    )
    for item in (*target.stock_locations, *source.stock_locations):
        row = combined[item.location_id]
        row["location_id"] = item.location_id
        row["location_name"] = item.location_name
        row["amount"] += item.amount
    return [
        {
            "location_id": row["location_id"],
            "location_name": row["location_name"],
            "amount": _number(row["amount"]),
        }
        for row in sorted(combined.values(), key=lambda item: item["location_id"])
        if row["amount"] != 0
    ]


def _snapshot(lookup: ProductLookupResult, aliases: set[str]) -> dict[str, Any]:
    return {
        "product_id": lookup.product.id,
        "product_name": lookup.product.name,
        "quantity_unit": lookup.product.quantity_unit.as_dict(),
        "stock_total": _number(lookup.product.stock_total),
        "stock_locations": [item.as_dict() for item in lookup.stock_locations],
        "barcodes": [item.as_dict() for item in lookup.product.barcodes],
        "voice_aliases": sorted(aliases),
    }


class GrocyProductMergeManager:
    """Plan, execute, journal, and verify product merges."""

    def __init__(
        self,
        client: GrocyApiClient,
        resolver: GrocyProductResolver,
        transactions: GrocyTransactionManager,
        aliases: GrocyVoiceAliases,
        journal: TransactionJournal,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._transactions = transactions
        self._aliases = aliases
        self._journal = journal
        self._request_locks: defaultdict[str, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def _async_plan(
        self,
        product_id_to_keep: int,
        product_id_to_remove: int,
        canonical_name: str | None,
    ) -> dict[str, Any]:
        if product_id_to_keep == product_id_to_remove:
            raise MergeSameProductError

        target = await self._resolver.async_lookup_by_product_id(product_id_to_keep)
        source = await self._resolver.async_lookup_by_product_id(product_id_to_remove)
        if target.product.quantity_unit.id != source.product.quantity_unit.id:
            raise MergeQuantityUnitMismatchError

        target_fields = await self._client.async_get_product_userfields(
            product_id_to_keep
        )
        source_fields = await self._client.async_get_product_userfields(
            product_id_to_remove
        )
        target_aliases = set(
            parse_alias_value(target_fields.get(VOICE_ALIAS_USERFIELD))
        )
        source_aliases = set(
            parse_alias_value(source_fields.get(VOICE_ALIAS_USERFIELD))
        )
        merged_aliases = target_aliases | source_aliases

        products = await self._client.async_get_products()
        alias_index = await self._aliases.async_index(products)
        for alias in merged_aliases:
            if alias_index.get(alias, frozenset()) - {
                product_id_to_keep,
                product_id_to_remove,
            }:
                raise MergeAliasConflictError(alias)

        final_name = (canonical_name or target.product.name).strip()
        for product in products:
            try:
                product_id = int(product.get("id"))
            except (TypeError, ValueError):
                continue
            name = product.get("name")
            if (
                product_id not in {product_id_to_keep, product_id_to_remove}
                and isinstance(name, str)
                and name.strip().casefold() == final_name.casefold()
            ):
                raise MergeNameConflictError(final_name)

        expected_barcodes = sorted(
            {
                item.barcode
                for item in (*target.product.barcodes, *source.product.barcodes)
            }
        )
        expected_locations = _expected_stock(target, source)
        return {
            "target": _snapshot(target, target_aliases),
            "source": _snapshot(source, source_aliases),
            "expected": {
                "product_id": product_id_to_keep,
                "product_name": final_name,
                "stock_total": _number(
                    target.product.stock_total + source.product.stock_total
                ),
                "stock_locations": expected_locations,
                "barcodes": expected_barcodes,
                "voice_aliases": sorted(merged_aliases),
            },
        }

    async def async_execute(
        self,
        *,
        product_id_to_keep: int,
        product_id_to_remove: int,
        canonical_name: str | None,
        request_id: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Return a plan or execute one idempotent, verified native merge."""
        fingerprint = json.dumps(
            {
                "operation": "merge_products",
                "product_id_to_keep": product_id_to_keep,
                "product_id_to_remove": product_id_to_remove,
                "canonical_name": canonical_name,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        async with self._request_locks[request_id]:
            if not dry_run:
                prior = await self._journal.async_get(request_id)
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise TransactionRequestConflictError
                    replay = dict(prior["result"])
                    replay["replayed"] = True
                    return replay

            async with self._transactions.async_lock_products(
                product_id_to_keep, product_id_to_remove
            ):
                plan = await self._async_plan(
                    product_id_to_keep,
                    product_id_to_remove,
                    canonical_name,
                )
                base = {
                    "response_version": 1,
                    "operation": "merge_products",
                    "request_id": request_id,
                    "replayed": False,
                    "dry_run": dry_run,
                    **plan,
                }
                if dry_run:
                    return {
                        **base,
                        "success": True,
                        "outcome": "planned",
                        "stock_changed": False,
                        "requires_reconciliation": False,
                    }

                pending = {
                    **base,
                    "success": False,
                    "outcome": "unknown",
                    "stock_changed": None,
                    "requires_reconciliation": True,
                    "uncertainty_reason": "in_progress_or_interrupted",
                }
                await self._journal.async_record(request_id, fingerprint, pending)

                original_aliases = set(plan["target"]["voice_aliases"])
                merged_aliases = set(plan["expected"]["voice_aliases"])
                await self._async_set_aliases_verified(
                    product_id_to_keep, merged_aliases
                )

                merge_error: GrocyApiError | None = None
                try:
                    await self._client.async_merge_products(
                        product_id_to_keep, product_id_to_remove
                    )
                except GrocyMutationOutcomeUnknownError as err:
                    merge_error = err
                except GrocyApiError as err:
                    merge_error = err

                source_exists = await self._async_product_exists(product_id_to_remove)
                if source_exists:
                    aliases_restored = await self._async_try_restore_aliases(
                        product_id_to_keep, original_aliases
                    )
                    result = {
                        **base,
                        "success": False,
                        "outcome": "not_committed",
                        "stock_changed": False,
                        "requires_reconciliation": not aliases_restored,
                        "error_code": "grocy_merge_rejected",
                        "message": "Grocy did not merge the products.",
                    }
                    await self._journal.async_record(request_id, fingerprint, result)
                    if merge_error is not None and not aliases_restored:
                        raise merge_error
                    return result

                final_name = plan["expected"]["product_name"]
                await self._async_apply_name(product_id_to_keep, final_name)
                verification = await self._async_verify(plan["expected"])
                result = {
                    **base,
                    "success": verification["verified"],
                    "outcome": (
                        "committed" if verification["verified"] else "unknown"
                    ),
                    "stock_changed": True,
                    "requires_reconciliation": not verification["verified"],
                    "verification": verification,
                    "uncertainty_reason": (
                        None if verification["verified"] else "verification_mismatch"
                    ),
                }
                await self._journal.async_record(request_id, fingerprint, result)
                return result

    async def _async_set_aliases_verified(
        self, product_id: int, aliases: set[str]
    ) -> None:
        value = _serialise_aliases(aliases)
        write_error: GrocyMutationOutcomeUnknownError | None = None
        try:
            await self._client.async_set_product_userfield(
                product_id, VOICE_ALIAS_USERFIELD, value
            )
        except GrocyMutationOutcomeUnknownError as err:
            write_error = err
        fields = await self._client.async_get_product_userfields(product_id)
        if set(parse_alias_value(fields.get(VOICE_ALIAS_USERFIELD))) != aliases:
            if write_error is not None:
                raise write_error
            raise GrocyMutationOutcomeUnknownError

    async def _async_try_restore_aliases(
        self, product_id: int, aliases: set[str]
    ) -> bool:
        try:
            await self._async_set_aliases_verified(product_id, aliases)
        except GrocyApiError:
            return False
        return True

    async def _async_product_exists(self, product_id: int) -> bool:
        try:
            await self._resolver.async_lookup_by_product_id(product_id)
        except GrocyNotFoundError:
            return False
        return True

    async def _async_apply_name(self, product_id: int, expected_name: str) -> None:
        lookup = await self._resolver.async_lookup_by_product_id(product_id)
        if lookup.product.name == expected_name:
            return
        write_error: GrocyMutationOutcomeUnknownError | None = None
        try:
            await self._client.async_update_product(
                product_id, {"name": expected_name}
            )
        except GrocyMutationOutcomeUnknownError as err:
            write_error = err
        verified = await self._resolver.async_lookup_by_product_id(product_id)
        if verified.product.name != expected_name:
            if write_error is not None:
                raise write_error
            raise GrocyMutationOutcomeUnknownError

    async def _async_verify(self, expected: dict[str, Any]) -> dict[str, Any]:
        lookup = await self._resolver.async_lookup_by_product_id(
            expected["product_id"]
        )
        fields = await self._client.async_get_product_userfields(
            expected["product_id"]
        )
        actual_locations = [
            {
                "location_id": row["location_id"],
                "location_name": row["location_name"],
                "amount": _number(row["amount"]),
            }
            for row in sorted(
                _stock_by_location(lookup).values(),
                key=lambda item: item["location_id"],
            )
        ]
        actual = {
            "product_id": lookup.product.id,
            "product_name": lookup.product.name,
            "stock_total": _number(lookup.product.stock_total),
            "stock_locations": actual_locations,
            "barcodes": sorted(item.barcode for item in lookup.product.barcodes),
            "voice_aliases": sorted(
                parse_alias_value(fields.get(VOICE_ALIAS_USERFIELD))
            ),
        }
        checks = {
            key: actual[key] == expected[key]
            for key in (
                "product_id",
                "product_name",
                "stock_total",
                "stock_locations",
                "barcodes",
                "voice_aliases",
            )
        }
        return {"verified": all(checks.values()), "checks": checks, "actual": actual}

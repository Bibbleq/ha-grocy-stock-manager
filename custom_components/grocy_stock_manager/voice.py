"""Fail-closed natural-language product resolution for garage stock."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from .api import (
    GrocyApiClient,
    GrocyApiError,
    GrocyInvalidResponseError,
    GrocyMutationOutcomeUnknownError,
)
from .const import VOICE_ALIAS_USERFIELD, VOICE_CONFIRMATION_TTL_SECONDS
from .models import ProductLookupResult
from .resolver import GrocyProductResolver
from .transactions import GrocyTransactionManager

type VoiceOperation = Literal["add", "consume"]
type ResolutionStatus = Literal[
    "resolved", "needs_confirmation", "ambiguous", "unknown"
]

_NON_WORD = re.compile(r"[^a-z0-9]+")
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_STOP_WORDS = frozenset({"a", "an", "and", "of", "the"})
_MINIMUM_CANDIDATE_SCORE = 0.35
_AMBIGUITY_MARGIN = 0.12


class VoiceAliasConflictError(Exception):
    """Raised when a learned phrase belongs to more than one product."""


class VoiceAliasFieldMissingError(Exception):
    """Raised when Grocy has no product voice_aliases userfield."""


class VoiceConfirmationNotFoundError(Exception):
    """Raised when a confirmation token is unknown or expired."""


class VoiceCandidateNotAllowedError(Exception):
    """Raised when confirmation names a product outside the offered candidates."""


def normalise_product_phrase(value: str) -> str:
    """Return a stable comparison form for spoken product text."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(_NON_WORD.sub(" ", ascii_value).split())


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in normalise_product_phrase(value).split()
        if token not in _STOP_WORDS
    )


def _base_name(value: str) -> str:
    return normalise_product_phrase(_PARENTHETICAL.sub(" ", value))


def _candidate_score(phrase: str, product_name: str) -> float:
    """Rank a possible product without ever treating the score as authority."""
    normalised_name = normalise_product_phrase(product_name)
    if phrase == normalised_name:
        return 1.0
    if phrase == _base_name(product_name):
        return 0.98

    phrase_tokens = _tokens(phrase)
    name_tokens = _tokens(normalised_name)
    if not phrase_tokens or not name_tokens:
        return 0.0
    common = phrase_tokens & name_tokens
    if not common:
        return SequenceMatcher(None, phrase, normalised_name).ratio() * 0.25
    coverage = len(common) / len(phrase_tokens)
    precision = len(common) / len(name_tokens)
    sequence = SequenceMatcher(None, phrase, normalised_name).ratio()
    return (coverage * 0.55) + (precision * 0.20) + (sequence * 0.25)


def _summary(payload: Mapping[str, Any]) -> tuple[int, str] | None:
    """Return one usable active product summary."""
    try:
        product_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return None
    name = payload.get("name")
    active = payload.get("active", 1)
    try:
        is_active = int(active) != 0
    except (TypeError, ValueError):
        is_active = bool(active)
    if product_id < 1 or not isinstance(name, str) or not name.strip() or not is_active:
        return None
    return product_id, name.strip()


def _parse_alias_value(value: object) -> tuple[str, ...]:
    """Decode aliases from human-readable text or the legacy JSON format."""
    if value in (None, ""):
        return ()
    if not isinstance(value, str):
        raise GrocyInvalidResponseError
    stripped = value.strip()
    if not stripped:
        return ()
    if stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError) as err:
            raise GrocyInvalidResponseError from err
        if not isinstance(decoded, list) or not all(
            isinstance(item, str) for item in decoded
        ):
            raise GrocyInvalidResponseError
        raw_aliases = decoded
    else:
        raw_aliases = re.split(r"[\r\n,]+", stripped)
    aliases = {
        normalised
        for item in raw_aliases
        if (normalised := normalise_product_phrase(item))
    }
    return tuple(sorted(aliases))


def _serialise_aliases(aliases: set[str]) -> str:
    """Store one normalised alias per line for easy Grocy UI editing."""
    return "\n".join(sorted(aliases))


def _product_aliases(product: Mapping[str, Any]) -> tuple[str, ...]:
    fields = product.get("userfields")
    if fields is None:
        return ()
    if not isinstance(fields, Mapping):
        raise GrocyInvalidResponseError
    return _parse_alias_value(fields.get(VOICE_ALIAS_USERFIELD))


class GrocyVoiceAliases:
    """Read, merge, write, and verify product aliases stored in Grocy."""

    def __init__(self, client: GrocyApiClient) -> None:
        self._client = client
        self._lock = asyncio.Lock()

    @staticmethod
    def _field_is_configured(products: Sequence[Mapping[str, Any]]) -> bool:
        return any(
            isinstance(product.get("userfields"), Mapping)
            and VOICE_ALIAS_USERFIELD in product["userfields"]
            for product in products
        )

    @staticmethod
    def _index(
        products: Sequence[Mapping[str, Any]],
    ) -> dict[str, frozenset[int]]:
        index: defaultdict[str, set[int]] = defaultdict(set)
        for product in products:
            parsed = _summary(product)
            if parsed is None:
                continue
            product_id, _name = parsed
            for alias in _product_aliases(product):
                index[alias].add(product_id)
        return {
            phrase: frozenset(product_ids)
            for phrase, product_ids in sorted(index.items())
        }

    async def async_index(
        self,
        products: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, frozenset[int]]:
        """Return every normalised alias and all products claiming it."""
        if products is None:
            products = await self._client.async_get_products()
        return self._index(products)

    async def async_learn(self, phrase: str, product_id: int) -> bool:
        """Add an alias to one product, rejecting reassignment and verifying it."""
        normalised = normalise_product_phrase(phrase)
        if not normalised:
            raise GrocyInvalidResponseError
        async with self._lock:
            products = await self._client.async_get_products()
            if not self._field_is_configured(products):
                raise VoiceAliasFieldMissingError
            product_ids = {
                parsed[0]
                for product in products
                if (parsed := _summary(product)) is not None
            }
            if product_id not in product_ids:
                raise GrocyInvalidResponseError
            existing_ids = self._index(products).get(normalised, frozenset())
            if existing_ids - {product_id}:
                raise VoiceAliasConflictError(normalised)
            if existing_ids == {product_id}:
                return False

            fields = await self._client.async_get_product_userfields(product_id)
            aliases = set(_parse_alias_value(fields.get(VOICE_ALIAS_USERFIELD)))
            aliases.add(normalised)
            value = _serialise_aliases(aliases)
            write_error: GrocyMutationOutcomeUnknownError | None = None
            try:
                await self._client.async_set_product_userfield(
                    product_id,
                    VOICE_ALIAS_USERFIELD,
                    value,
                )
            except GrocyMutationOutcomeUnknownError as err:
                write_error = err

            try:
                verified = await self._client.async_get_product_userfields(
                    product_id
                )
            except GrocyApiError as err:
                if write_error is not None:
                    raise write_error from err
                raise GrocyMutationOutcomeUnknownError from err
            if normalised not in _parse_alias_value(
                verified.get(VOICE_ALIAS_USERFIELD)
            ):
                if write_error is not None:
                    raise write_error
                raise GrocyMutationOutcomeUnknownError
            return True

    async def async_remove(self, phrase: str) -> bool:
        """Remove an alias from its unique product and verify the change."""
        normalised = normalise_product_phrase(phrase)
        async with self._lock:
            products = await self._client.async_get_products()
            if not self._field_is_configured(products):
                raise VoiceAliasFieldMissingError
            product_ids = self._index(products).get(normalised, frozenset())
            if not product_ids:
                return False
            if len(product_ids) != 1:
                raise VoiceAliasConflictError(normalised)
            product_id = next(iter(product_ids))
            fields = await self._client.async_get_product_userfields(product_id)
            aliases = set(_parse_alias_value(fields.get(VOICE_ALIAS_USERFIELD)))
            aliases.discard(normalised)
            value = _serialise_aliases(aliases)
            write_error: GrocyMutationOutcomeUnknownError | None = None
            try:
                await self._client.async_set_product_userfield(
                    product_id,
                    VOICE_ALIAS_USERFIELD,
                    value,
                )
            except GrocyMutationOutcomeUnknownError as err:
                write_error = err

            try:
                verified = await self._client.async_get_product_userfields(
                    product_id
                )
            except GrocyApiError as err:
                if write_error is not None:
                    raise write_error from err
                raise GrocyMutationOutcomeUnknownError from err
            if normalised in _parse_alias_value(verified.get(VOICE_ALIAS_USERFIELD)):
                if write_error is not None:
                    raise write_error
                raise GrocyMutationOutcomeUnknownError
            return True

    async def async_list(self) -> list[dict[str, Any]]:
        """Return aliases with conflict information and canonical product names."""
        products = await self._client.async_get_products()
        names = {
            parsed[0]: parsed[1]
            for product in products
            if (parsed := _summary(product)) is not None
        }
        return [
            {
                "product_phrase": phrase,
                "products": [
                    {"product_id": product_id, "product_name": names[product_id]}
                    for product_id in sorted(product_ids)
                    if product_id in names
                ],
                "conflict": len(product_ids) != 1,
            }
            for phrase, product_ids in self._index(products).items()
        ]


@dataclass(frozen=True, slots=True)
class VoiceProductCandidate:
    """One ranked product, enriched with authoritative current stock."""

    lookup: ProductLookupResult
    score: float
    match_type: str

    def as_dict(self) -> dict[str, Any]:
        product = self.lookup.product
        return {
            "product_id": product.id,
            "product_name": product.name,
            "score": round(self.score, 3),
            "match_type": self.match_type,
            "quantity_unit": product.quantity_unit.as_dict(),
            "stock_total": float(product.stock_total),
            "stock_locations": [
                location.as_dict() for location in self.lookup.stock_locations
            ],
            "default_location": (
                product.default_location.as_dict()
                if product.default_location is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class VoiceResolution:
    """A fail-closed product phrase resolution result."""

    status: ResolutionStatus
    phrase: str
    normalised_phrase: str
    operation: VoiceOperation
    candidates: tuple[VoiceProductCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "response_version": 1,
            "success": self.status == "resolved",
            "status": self.status,
            "product_phrase": self.phrase,
            "normalised_phrase": self.normalised_phrase,
            "operation": self.operation,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


class GrocyVoiceResolver:
    """Resolve spoken text against live Grocy products and Grocy aliases."""

    def __init__(
        self,
        client: GrocyApiClient,
        resolver: GrocyProductResolver,
        aliases: GrocyVoiceAliases,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._aliases = aliases

    async def _async_candidate(
        self,
        product_id: int,
        score: float,
        match_type: str,
    ) -> VoiceProductCandidate:
        lookup = await self._resolver.async_lookup_by_product_id(product_id)
        return VoiceProductCandidate(lookup, score, match_type)

    async def _async_candidates(
        self,
        candidates: Sequence[tuple[int, float, str]],
    ) -> tuple[VoiceProductCandidate, ...]:
        return tuple(
            await asyncio.gather(
                *(
                    self._async_candidate(product_id, score, match_type)
                    for product_id, score, match_type in candidates
                )
            )
        )

    async def async_resolve(
        self,
        phrase: str,
        *,
        operation: VoiceOperation,
        candidate_limit: int = 3,
    ) -> VoiceResolution:
        """Resolve a phrase; only exact canonical or unique aliases are authority."""
        phrase = phrase.strip()
        normalised = normalise_product_phrase(phrase)
        products_payload = await self._client.async_get_products()
        products = [
            parsed
            for item in products_payload
            if (parsed := _summary(item)) is not None
        ]
        if not products:
            return VoiceResolution("unknown", phrase, normalised, operation, ())

        alias_ids = (await self._aliases.async_index(products_payload)).get(
            normalised, frozenset()
        )
        if alias_ids:
            candidates = await self._async_candidates(
                [
                    (product_id, 1.0, "learned_alias")
                    for product_id in sorted(alias_ids)[:candidate_limit]
                ]
            )
            status: ResolutionStatus = (
                "resolved" if len(alias_ids) == 1 else "ambiguous"
            )
            return VoiceResolution(status, phrase, normalised, operation, candidates)

        exact = [
            item
            for item in products
            if normalised
            in {normalise_product_phrase(item[1]), _base_name(item[1])}
        ]
        if exact:
            candidates = await self._async_candidates(
                [
                    (
                        product_id,
                        _candidate_score(normalised, name),
                        "canonical_exact",
                    )
                    for product_id, name in exact[:candidate_limit]
                ]
            )
            status = "resolved" if len(exact) == 1 else "ambiguous"
            return VoiceResolution(status, phrase, normalised, operation, candidates)

        ranked = sorted(
            (
                (_candidate_score(normalised, name), product_id, name)
                for product_id, name in products
            ),
            key=lambda item: (-item[0], item[2].casefold(), item[1]),
        )
        shortlisted = [
            item
            for item in ranked[: max(candidate_limit * 2, candidate_limit)]
            if item[0] >= _MINIMUM_CANDIDATE_SCORE
        ]
        if not shortlisted:
            return VoiceResolution("unknown", phrase, normalised, operation, ())

        enriched = list(
            await self._async_candidates(
                [
                    (product_id, score, "name_similarity")
                    for score, product_id, _name in shortlisted
                ]
            )
        )
        if operation == "consume":
            stocked = [
                candidate
                for candidate in enriched
                if candidate.lookup.product.stock_total > 0
            ]
            if stocked:
                enriched = stocked
        enriched.sort(
            key=lambda item: (
                -item.score,
                item.lookup.product.name.casefold(),
                item.lookup.product.id,
            )
        )
        candidates = tuple(enriched[:candidate_limit])
        if not candidates:
            return VoiceResolution("unknown", phrase, normalised, operation, ())
        if len(candidates) == 1:
            status = "needs_confirmation"
        else:
            margin = candidates[0].score - candidates[1].score
            status = (
                "needs_confirmation" if margin >= _AMBIGUITY_MARGIN else "ambiguous"
            )
        return VoiceResolution(status, phrase, normalised, operation, candidates)


@dataclass(frozen=True, slots=True)
class PendingVoiceTransaction:
    """Short-lived transaction state; expiry can only prevent a write."""

    created_at: float
    operation: VoiceOperation
    product_phrase: str
    amount: Decimal
    request_id: str
    location_id: int | None
    location_name: str | None
    source: str
    candidate_ids: frozenset[int]


class GrocyVoiceManager:
    """Orchestrate resolution, guarded writes, and explicit confirmations."""

    def __init__(
        self,
        resolver: GrocyVoiceResolver,
        product_resolver: GrocyProductResolver,
        transactions: GrocyTransactionManager,
        aliases: GrocyVoiceAliases,
    ) -> None:
        self._resolver = resolver
        self._product_resolver = product_resolver
        self._transactions = transactions
        self._aliases = aliases
        self._pending: dict[str, PendingVoiceTransaction] = {}
        self._pending_lock = asyncio.Lock()

    async def async_process(
        self,
        *,
        operation: VoiceOperation,
        product_phrase: str,
        amount: Decimal,
        request_id: str,
        location_id: int | None,
        location_name: str | None,
        source: str,
        candidate_limit: int,
    ) -> dict[str, Any]:
        """Commit only authoritative matches; stage every uncertain match."""
        resolution = await self._resolver.async_resolve(
            product_phrase,
            operation=operation,
            candidate_limit=candidate_limit,
        )
        if resolution.status == "resolved":
            transaction = await self._transactions.async_execute(
                operation,
                resolution.candidates[0].lookup,
                amount=amount,
                request_id=request_id,
                location_id=location_id,
                location_name=location_name,
                source=source,
            )
            return {
                **resolution.as_dict(),
                "success": True,
                "status": "committed",
                "stock_changed": transaction.get("outcome") == "committed",
                "transaction": transaction,
            }

        response = resolution.as_dict()
        response["stock_changed"] = False
        if resolution.candidates:
            token = uuid4().hex
            pending = PendingVoiceTransaction(
                created_at=monotonic(),
                operation=operation,
                product_phrase=product_phrase.strip(),
                amount=amount,
                request_id=request_id,
                location_id=location_id,
                location_name=location_name,
                source=source,
                candidate_ids=frozenset(
                    candidate.lookup.product.id for candidate in resolution.candidates
                ),
            )
            async with self._pending_lock:
                self._purge_expired()
                self._pending[token] = pending
            response["confirmation_token"] = token
            response["confirmation_expires_in"] = VOICE_CONFIRMATION_TTL_SECONDS
        return response

    async def async_confirm(
        self,
        *,
        confirmation_token: str,
        product_id: int,
        learn_alias: bool,
    ) -> dict[str, Any]:
        """Confirm one offered product, optionally learn it, then transact."""
        async with self._pending_lock:
            self._purge_expired()
            pending = self._pending.get(confirmation_token)
            if pending is None:
                raise VoiceConfirmationNotFoundError
            if product_id not in pending.candidate_ids:
                raise VoiceCandidateNotAllowedError
            self._pending.pop(confirmation_token)

        lookup = await self._product_resolver.async_lookup_by_product_id(product_id)
        alias_learned = False
        if learn_alias:
            alias_learned = await self._aliases.async_learn(
                pending.product_phrase,
                product_id,
            )
        transaction = await self._transactions.async_execute(
            pending.operation,
            lookup,
            amount=pending.amount,
            request_id=pending.request_id,
            location_id=pending.location_id,
            location_name=pending.location_name,
            source=pending.source,
        )
        return {
            "response_version": 1,
            "success": True,
            "status": "committed",
            "stock_changed": transaction.get("outcome") == "committed",
            "product_phrase": pending.product_phrase,
            "product_id": lookup.product.id,
            "product_name": lookup.product.name,
            "alias_learned": alias_learned,
            "transaction": transaction,
        }

    def _purge_expired(self) -> None:
        cutoff = monotonic() - VOICE_CONFIRMATION_TTL_SECONDS
        expired = [
            token
            for token, pending in self._pending.items()
            if pending.created_at < cutoff
        ]
        for token in expired:
            self._pending.pop(token, None)

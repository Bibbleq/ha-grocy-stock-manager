"""Durable, expiring confirmation intents for voice transactions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    VOICE_CONFIRMATION_TTL_SECONDS,
    VOICE_STORAGE_KEY,
    VOICE_STORAGE_VERSION,
)


@dataclass(frozen=True, slots=True)
class PendingVoiceTransaction:
    """One immutable voice request awaiting a product choice."""

    created_at: float
    operation: str
    product_phrase: str
    amount: Decimal
    request_id: str
    location_id: int | None
    location_name: str | None
    source: str
    candidate_ids: frozenset[int]

    def as_storage_dict(self) -> dict[str, Any]:
        """Return a stable JSON-safe storage representation."""
        payload = asdict(self)
        payload["amount"] = format(self.amount, "f")
        payload["candidate_ids"] = sorted(self.candidate_ids)
        return payload

    @classmethod
    def from_storage_dict(cls, payload: Mapping[str, Any]) -> PendingVoiceTransaction:
        """Validate and restore one persisted request."""
        try:
            created_at = float(payload["created_at"])
            amount = Decimal(str(payload["amount"]))
            candidate_ids = frozenset(int(item) for item in payload["candidate_ids"])
            operation = str(payload["operation"])
            product_phrase = str(payload["product_phrase"])
            request_id = str(payload["request_id"])
            source = str(payload["source"])
            raw_location_id = payload.get("location_id")
            location_id = int(raw_location_id) if raw_location_id is not None else None
            raw_location_name = payload.get("location_name")
            location_name = (
                str(raw_location_name) if raw_location_name is not None else None
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as err:
            raise ValueError from err
        if (
            operation not in {"add", "consume"}
            or not product_phrase
            or not request_id
            or not source
            or not candidate_ids
            or not amount.is_finite()
            or amount <= 0
        ):
            raise ValueError
        return cls(
            created_at=created_at,
            operation=operation,
            product_phrase=product_phrase,
            amount=amount,
            request_id=request_id,
            location_id=location_id,
            location_name=location_name,
            source=source,
            candidate_ids=candidate_ids,
        )


class VoiceConfirmationStore:
    """Persist pending confirmations so an HA restart cannot lose intent."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](
            hass, VOICE_STORAGE_VERSION, VOICE_STORAGE_KEY
        )
        self._records: dict[str, PendingVoiceTransaction] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def now_timestamp() -> float:
        """Return a wall-clock timestamp suitable for durable expiry."""
        return datetime.now(UTC).timestamp()

    async def async_load(self) -> None:
        """Load valid, unexpired confirmation records."""
        stored = await self._store.async_load()
        if isinstance(stored, Mapping):
            raw_records = stored.get("records")
            if isinstance(raw_records, Mapping):
                for token, payload in raw_records.items():
                    if not isinstance(token, str) or not isinstance(payload, Mapping):
                        continue
                    try:
                        pending = PendingVoiceTransaction.from_storage_dict(payload)
                        self._records[token] = pending
                    except ValueError:
                        continue
        self._purge_expired()
        await self._async_save()

    async def async_put(
        self, token: str, pending: PendingVoiceTransaction
    ) -> None:
        """Persist one new immutable confirmation intent."""
        async with self._lock:
            self._purge_expired()
            self._records[token] = pending
            await self._async_save()

    async def async_take(
        self, token: str, product_id: int
    ) -> PendingVoiceTransaction | None:
        """Atomically consume a token only for one of its offered candidates."""
        async with self._lock:
            self._purge_expired()
            pending = self._records.get(token)
            if pending is None or product_id not in pending.candidate_ids:
                return pending
            self._records.pop(token)
            await self._async_save()
            return pending

    def snapshot(self) -> list[dict[str, Any]]:
        """Return unexpired metadata for status entities without exposing tokens."""
        now = self.now_timestamp()
        records = []
        for pending in self._records.values():
            remaining = max(
                0,
                int(
                    VOICE_CONFIRMATION_TTL_SECONDS
                    - (now - pending.created_at)
                ),
            )
            if remaining == 0:
                continue
            records.append(
                {
                    "operation": pending.operation,
                    "product_phrase": pending.product_phrase,
                    "amount": float(pending.amount),
                    "request_id": pending.request_id,
                    "source": pending.source,
                    "candidate_ids": sorted(pending.candidate_ids),
                    "expires_in": remaining,
                }
            )
        return records

    def _purge_expired(self) -> None:
        cutoff = self.now_timestamp() - VOICE_CONFIRMATION_TTL_SECONDS
        for token in [
            token
            for token, pending in self._records.items()
            if pending.created_at < cutoff
        ]:
            self._records.pop(token, None)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "records": {
                    token: pending.as_storage_dict()
                    for token, pending in self._records.items()
                }
            }
        )

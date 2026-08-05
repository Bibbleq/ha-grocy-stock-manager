"""Durable idempotency journal for Grocy stock mutations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import MAX_JOURNAL_RECORDS, STORAGE_KEY, STORAGE_VERSION


class TransactionJournal:
    """Persist completed or uncertain request outcomes across HA restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the journal."""
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load valid journal records, discarding malformed storage data."""
        stored = await self._store.async_load()
        if not isinstance(stored, Mapping):
            return
        raw_records = stored.get("records")
        if not isinstance(raw_records, list):
            return
        for item in raw_records[-MAX_JOURNAL_RECORDS:]:
            if not isinstance(item, dict):
                continue
            request_id = item.get("request_id")
            fingerprint = item.get("fingerprint")
            result = item.get("result")
            if (
                isinstance(request_id, str)
                and isinstance(fingerprint, str)
                and isinstance(result, dict)
            ):
                self._records[request_id] = {
                    "request_id": request_id,
                    "fingerprint": fingerprint,
                    "result": result,
                }

    async def async_get(self, request_id: str) -> dict[str, Any] | None:
        """Return one immutable-copy record if it exists."""
        async with self._lock:
            record = self._records.get(request_id)
            return dict(record) if record is not None else None

    async def async_record(
        self, request_id: str, fingerprint: str, result: dict[str, Any]
    ) -> None:
        """Record and persist one terminal request outcome."""
        async with self._lock:
            self._records.pop(request_id, None)
            self._records[request_id] = {
                "request_id": request_id,
                "fingerprint": fingerprint,
                "result": dict(result),
            }
            while len(self._records) > MAX_JOURNAL_RECORDS:
                self._records.popitem(last=False)
            await self._store.async_save({"records": list(self._records.values())})

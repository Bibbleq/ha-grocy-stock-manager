"""Durable idempotency journal for Grocy stock mutations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import MAX_JOURNAL_RECORDS, STORAGE_KEY, STORAGE_VERSION

_UNDOABLE_SOURCES = frozenset(
    {
        "dashboard",
        "garage_scanner",
        "garage_voice",
        "home_assistant",
        "home_assistant_confirm",
        "tablet",
        "voice",
    }
)


def is_undoable_result(result: Mapping[str, Any]) -> bool:
    """Return whether one verified family-facing mutation may be compensated."""
    source = result.get("source")
    return (
        result.get("outcome") == "committed"
        and result.get("operation") in {"add", "consume"}
        and not result.get("requires_reconciliation")
        and not result.get("undo_of")
        and not result.get("undone_by")
        and isinstance(source, str)
        and (source in _UNDOABLE_SOURCES or source.startswith("garage_"))
    )


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
                    "recorded_at": item.get("recorded_at"),
                }

    async def async_get(self, request_id: str) -> dict[str, Any] | None:
        """Return one immutable-copy record if it exists."""
        async with self._lock:
            record = self._records.get(request_id)
            return deepcopy(record) if record is not None else None

    def snapshot(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest journal results for entities and diagnostics."""
        records = list(self._records.values())[-limit:]
        return [
            {
                "request_id": item["request_id"],
                "recorded_at": item.get("recorded_at"),
                **deepcopy(item["result"]),
            }
            for item in reversed(records)
        ]

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
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            while len(self._records) > MAX_JOURNAL_RECORDS:
                self._records.popitem(last=False)
            await self._store.async_save({"records": list(self._records.values())})

    async def async_update_result(
        self, request_id: str, changes: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Annotate an existing result without changing its idempotency fingerprint."""
        async with self._lock:
            record = self._records.get(request_id)
            if record is None:
                return None
            result = dict(record["result"])
            result.update(changes)
            record["result"] = result
            await self._store.async_save({"records": list(self._records.values())})
            return deepcopy(result)

    async def async_acknowledge_reconciliation(
        self, request_id: str, note: str
    ) -> dict[str, Any] | None:
        """Record an explicit human reconciliation of an uncertain outcome."""
        record = await self.async_get(request_id)
        if record is None or not record["result"].get("requires_reconciliation"):
            return None
        return await self.async_update_result(
            request_id,
            {
                "requires_reconciliation": False,
                "reconciled_at": datetime.now(UTC).isoformat(),
                "reconciliation_note": note,
            },
        )

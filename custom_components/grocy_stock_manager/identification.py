"""Durable asynchronous product identification jobs."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_IDENTIFICATION_AGENT,
    EVENT_IDENTIFICATION_UPDATED,
    IDENTIFICATION_STORAGE_KEY,
    IDENTIFICATION_STORAGE_VERSION,
    IDENTIFICATION_TIMEOUT_SECONDS,
    MAX_IDENTIFICATION_RECORDS,
)

if TYPE_CHECKING:
    from . import GrocyStockManagerConfigEntry

type IdentificationStatus = Literal[
    "searching", "ready", "manual_required", "completed", "rejected"
]

_TERMINAL_STATUSES = frozenset({"completed", "rejected"})
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TRAILING_CITATION = re.compile(r"\s*(?:\[[^\]]+\]|https?://\S+)\s*$")


class IdentificationRequestConflictError(Exception):
    """Raised when one request ID is reused for different scanner work."""


@dataclass(frozen=True, slots=True)
class ProductIdentificationJob:
    """One immutable scanner intent awaiting a product identity."""

    job_id: str
    created_at: float
    updated_at: float
    status: IdentificationStatus
    stage: str
    barcode: str
    operation: str
    amount: Decimal
    request_id: str
    location_id: int | None
    location_name: str | None
    quantity_unit_id: int | None
    quantity_unit_name: str | None
    source: str
    agent_id: str
    candidate_name: str | None = None
    error_code: str | None = None
    message: str | None = None
    elapsed_seconds: float | None = None

    def as_storage_dict(self) -> dict[str, Any]:
        """Return a JSON-safe storage representation."""
        payload = asdict(self)
        payload["amount"] = format(self.amount, "f")
        return payload

    def as_public_dict(self) -> dict[str, Any]:
        """Return safe job data for services, events and entities."""
        payload = self.as_storage_dict()
        payload["amount"] = float(self.amount)
        payload["created_at"] = datetime.fromtimestamp(
            self.created_at, UTC
        ).isoformat()
        payload["updated_at"] = datetime.fromtimestamp(
            self.updated_at, UTC
        ).isoformat()
        return payload

    @classmethod
    def from_storage_dict(
        cls, payload: Mapping[str, Any]
    ) -> ProductIdentificationJob:
        """Validate and restore one persisted job."""
        try:
            status = str(payload["status"])
            operation = str(payload["operation"])
            amount = Decimal(str(payload["amount"]))
            raw_location_id = payload.get("location_id")
            raw_quantity_unit_id = payload.get("quantity_unit_id")
            elapsed = payload.get("elapsed_seconds")
            job = cls(
                job_id=str(payload["job_id"]),
                created_at=float(payload["created_at"]),
                updated_at=float(payload["updated_at"]),
                status=status,  # type: ignore[arg-type]
                stage=str(payload["stage"]),
                barcode=str(payload["barcode"]),
                operation=operation,
                amount=amount,
                request_id=str(payload["request_id"]),
                location_id=(
                    int(raw_location_id) if raw_location_id is not None else None
                ),
                location_name=_optional_string(payload.get("location_name")),
                quantity_unit_id=(
                    int(raw_quantity_unit_id)
                    if raw_quantity_unit_id is not None
                    else None
                ),
                quantity_unit_name=_optional_string(
                    payload.get("quantity_unit_name")
                ),
                source=str(payload["source"]),
                agent_id=str(
                    payload.get("agent_id") or DEFAULT_IDENTIFICATION_AGENT
                ),
                candidate_name=_optional_string(payload.get("candidate_name")),
                error_code=_optional_string(payload.get("error_code")),
                message=_optional_string(payload.get("message")),
                elapsed_seconds=float(elapsed) if elapsed is not None else None,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as err:
            raise ValueError from err
        if (
            status
            not in {
                "searching",
                "ready",
                "manual_required",
                "completed",
                "rejected",
            }
            or operation not in {"add", "consume"}
            or not job.job_id
            or not job.barcode
            or not job.request_id
            or not job.source
            or not amount.is_finite()
            or amount <= 0
        ):
            raise ValueError
        return job


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class ProductIdentificationStore:
    """Persist identification jobs so restarts cannot lose scanner intent."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](
            hass, IDENTIFICATION_STORAGE_VERSION, IDENTIFICATION_STORAGE_KEY
        )
        self._records: dict[str, ProductIdentificationJob] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def now_timestamp() -> float:
        return datetime.now(UTC).timestamp()

    async def async_load(self) -> None:
        """Load every valid job, retaining unresolved work indefinitely."""
        stored = await self._store.async_load()
        if isinstance(stored, Mapping):
            raw_records = stored.get("records")
            if isinstance(raw_records, Mapping):
                for job_id, payload in raw_records.items():
                    if not isinstance(job_id, str) or not isinstance(
                        payload, Mapping
                    ):
                        continue
                    try:
                        job = ProductIdentificationJob.from_storage_dict(payload)
                    except ValueError:
                        continue
                    if job.job_id == job_id:
                        self._records[job_id] = job
        await self._async_save()

    async def async_create(
        self,
        *,
        barcode: str,
        operation: str,
        amount: Decimal,
        request_id: str,
        location_id: int | None,
        location_name: str | None,
        quantity_unit_id: int | None,
        quantity_unit_name: str | None,
        source: str,
        agent_id: str,
    ) -> tuple[ProductIdentificationJob, bool]:
        """Create a job or replay the existing request without duplication."""
        async with self._lock:
            existing = next(
                (
                    item
                    for item in self._records.values()
                    if item.request_id == request_id
                ),
                None,
            )
            if existing is not None:
                requested = (
                    barcode,
                    operation,
                    amount,
                    location_id,
                    location_name,
                    quantity_unit_id,
                    quantity_unit_name,
                    source,
                )
                original = (
                    existing.barcode,
                    existing.operation,
                    existing.amount,
                    existing.location_id,
                    existing.location_name,
                    existing.quantity_unit_id,
                    existing.quantity_unit_name,
                    existing.source,
                )
                if requested != original:
                    raise IdentificationRequestConflictError
                return existing, True
            self._prune_terminal_records()
            if len(self._records) >= MAX_IDENTIFICATION_RECORDS:
                raise RuntimeError("identification queue is full")
            now = self.now_timestamp()
            job = ProductIdentificationJob(
                job_id=uuid4().hex,
                created_at=now,
                updated_at=now,
                status="searching",
                stage="ai_lookup",
                barcode=barcode,
                operation=operation,
                amount=amount,
                request_id=request_id,
                location_id=location_id,
                location_name=location_name,
                quantity_unit_id=quantity_unit_id,
                quantity_unit_name=quantity_unit_name,
                source=source,
                agent_id=agent_id,
                message="Searching with AI",
            )
            self._records[job.job_id] = job
            await self._async_save()
            return job, False

    async def async_update(
        self,
        job_id: str,
        *,
        expected_statuses: frozenset[str] | None = None,
        **changes: Any,
    ) -> ProductIdentificationJob | None:
        """Atomically replace selected fields on one job."""
        async with self._lock:
            current = self._records.get(job_id)
            if current is None or (
                expected_statuses is not None
                and current.status not in expected_statuses
            ):
                return None
            updated = replace(
                current,
                updated_at=self.now_timestamp(),
                **changes,
            )
            self._records[job_id] = updated
            await self._async_save()
            return updated

    def get(self, job_id: str) -> ProductIdentificationJob | None:
        return self._records.get(job_id)

    def searching_jobs(self) -> tuple[ProductIdentificationJob, ...]:
        return tuple(
            item for item in self._records.values() if item.status == "searching"
        )

    def pending_snapshot(self) -> list[dict[str, Any]]:
        """Return unresolved work oldest-first for dashboard presentation."""
        return [
            item.as_public_dict()
            for item in sorted(
                self._records.values(), key=lambda value: value.created_at
            )
            if item.status not in _TERMINAL_STATUSES
        ][:25]

    def _prune_terminal_records(self) -> None:
        if len(self._records) < MAX_IDENTIFICATION_RECORDS:
            return
        terminal = sorted(
            (
                item
                for item in self._records.values()
                if item.status in _TERMINAL_STATUSES
            ),
            key=lambda item: item.updated_at,
        )
        for item in terminal:
            if len(self._records) < MAX_IDENTIFICATION_RECORDS:
                break
            self._records.pop(item.job_id, None)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "records": {
                    job_id: job.as_storage_dict()
                    for job_id, job in self._records.items()
                }
            }
        )


class ProductIdentificationManager:
    """Run slow AI lookups outside the scanner transaction path."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GrocyStockManagerConfigEntry,
        store: ProductIdentificationStore,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._store = store
        self._semaphore = asyncio.Semaphore(3)

    def async_resume(self) -> None:
        """Resume jobs interrupted by an HA restart."""
        for job in self._store.searching_jobs():
            self._fire_update(job)
            self._schedule(job.job_id)

    async def async_start(self, **data: Any) -> dict[str, Any]:
        """Persist an immutable intent and return before AI is invoked."""
        job, replayed = await self._store.async_create(**data)
        if not replayed and job.status == "searching":
            self._fire_update(job)
            self._schedule(job.job_id)
        return {
            "response_version": 1,
            "success": True,
            "accepted": True,
            "replayed": replayed,
            "job": job.as_public_dict(),
        }

    async def async_override(self, job_id: str) -> ProductIdentificationJob | None:
        """Move a searching or suggested job into immediate manual entry."""
        current = self._store.get(job_id)
        if current is None or current.status in _TERMINAL_STATUSES:
            return current
        updated = await self._store.async_update(
            job_id,
            expected_statuses=frozenset(
                {"searching", "ready", "manual_required"}
            ),
            status="manual_required",
            stage="manual_entry",
            candidate_name=None,
            error_code=None,
            message="Manual product entry requested",
        )
        if updated is not None:
            self._fire_update(updated)
        return updated

    async def async_complete(
        self, job_id: str, product_name: str
    ) -> ProductIdentificationJob | None:
        """Mark a job complete after the captured transaction was verified."""
        current = self._store.get(job_id)
        if current is None:
            return None
        updated = await self._store.async_update(
            job_id,
            expected_statuses=frozenset(
                {"searching", "ready", "manual_required"}
            ),
            status="completed",
            stage="completed",
            candidate_name=product_name,
            error_code=None,
            message="Product confirmed and captured transaction committed",
        )
        if updated is not None:
            self._fire_update(updated)
        return updated

    async def async_reject(self, job_id: str) -> ProductIdentificationJob | None:
        """Explicitly reject one pending identification without stock change."""
        current = self._store.get(job_id)
        if current is None:
            return None
        updated = await self._store.async_update(
            job_id,
            expected_statuses=frozenset(
                {"searching", "ready", "manual_required"}
            ),
            status="rejected",
            stage="rejected",
            error_code="rejected_by_user",
            message="Identification rejected; no stock was changed",
        )
        if updated is not None:
            self._fire_update(updated)
        return updated

    def _schedule(self, job_id: str) -> None:
        self._entry.async_create_background_task(
            self._hass,
            self._async_identify(job_id),
            f"Identify garage product {job_id}",
        )

    async def _async_identify(self, job_id: str) -> None:
        current = self._store.get(job_id)
        if current is None or current.status != "searching":
            return
        started = monotonic()
        try:
            async with self._semaphore, asyncio.timeout(
                IDENTIFICATION_TIMEOUT_SECONDS
            ):
                response = await self._hass.services.async_call(
                    "conversation",
                    "process",
                    {
                        "agent_id": current.agent_id,
                        "text": _identification_prompt(current.barcode),
                    },
                    blocking=True,
                    return_response=True,
                )
            candidate = _extract_candidate_name(response)
            if candidate is None:
                await self._finish_if_searching(
                    job_id,
                    status="manual_required",
                    stage="manual_entry",
                    error_code="ai_no_confident_match",
                    message="AI could not identify this barcode",
                    elapsed_seconds=round(monotonic() - started, 1),
                )
                return
            await self._finish_if_searching(
                job_id,
                status="ready",
                stage="confirmation",
                candidate_name=candidate,
                error_code=None,
                message="AI suggestion ready for confirmation",
                elapsed_seconds=round(monotonic() - started, 1),
            )
        except TimeoutError:
            await self._finish_if_searching(
                job_id,
                status="manual_required",
                stage="manual_entry",
                error_code="ai_timeout",
                message="AI lookup timed out; enter the product manually",
                elapsed_seconds=round(monotonic() - started, 1),
            )
        except Exception as err:  # Home Assistant service errors vary by agent.
            await self._finish_if_searching(
                job_id,
                status="manual_required",
                stage="manual_entry",
                error_code="ai_error",
                message=f"AI lookup failed ({type(err).__name__}); enter manually",
                elapsed_seconds=round(monotonic() - started, 1),
            )

    async def _finish_if_searching(
        self,
        job_id: str,
        **changes: Any,
    ) -> None:
        """Ignore late AI results after override, rejection or completion."""
        updated = await self._store.async_update(
            job_id,
            expected_statuses=frozenset({"searching"}),
            **changes,
        )
        if updated is not None:
            self._fire_update(updated)

    def _fire_update(self, job: ProductIdentificationJob) -> None:
        self._hass.bus.async_fire(
            EVENT_IDENTIFICATION_UPDATED,
            {"job": job.as_public_dict()},
        )
        self._entry.runtime_data.coordinator.async_update_listeners()


def _identification_prompt(barcode: str) -> str:
    return (
        "Identify this household or grocery product barcode for a UK home: "
        f"{barcode}. Search the web if needed and require an exact barcode "
        "match. Return only a short inventory label in the format "
        "'<type>, <brand>, <1-3 word descriptor>'. Do not include links, "
        "citations, instructions or commentary. If you are not confident, "
        "return exactly: unknown item"
    )


def _extract_candidate_name(response: object) -> str | None:
    """Extract and clean a short plain response from conversation.process."""
    if not isinstance(response, Mapping):
        return None
    speech = response.get("response")
    if isinstance(speech, Mapping):
        speech = speech.get("speech")
    if isinstance(speech, Mapping):
        speech = speech.get("plain")
    if isinstance(speech, Mapping):
        speech = speech.get("speech")
    if not isinstance(speech, str):
        return None
    candidate = _MARKDOWN_LINK.sub(r"\1", speech).strip().strip('"\'')
    candidate = _TRAILING_CITATION.sub("", candidate).strip(" ,.;")
    if (
        not candidate
        or candidate.casefold() == "unknown item"
        or candidate.casefold().startswith("error")
    ):
        return None
    return candidate[:255]

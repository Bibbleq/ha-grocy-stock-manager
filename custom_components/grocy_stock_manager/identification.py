"""Durable asynchronous product identification jobs."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
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
    from .journal import TransactionJournal
    from .voice import GrocyVoiceAliases

type IdentificationStatus = Literal[
    "searching",
    "ready",
    "manual_required",
    "confirming",
    "failed",
    "completed",
    "rejected",
]

_TERMINAL_STATUSES = frozenset({"completed", "rejected"})
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TRAILING_CITATION = re.compile(r"\s*(?:\[[^\]]+\]|https?://\S+)\s*$")
_LOGGER = logging.getLogger(__name__)


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
    confirmed_product_name: str | None = None
    confirmed_barcode_amount: Decimal | None = None
    accepted_aliases: tuple[str, ...] = ()

    @property
    def confirmation_request_id(self) -> str:
        """Return the stable transaction ID shared with the legacy HA flow."""
        base = (
            self.request_id[: -len(":identify")]
            if self.request_id.endswith(":identify")
            else self.request_id
        )
        return f"{base}:confirm"

    def as_storage_dict(self) -> dict[str, Any]:
        """Return a JSON-safe storage representation."""
        payload = asdict(self)
        payload["amount"] = format(self.amount, "f")
        if self.confirmed_barcode_amount is not None:
            payload["confirmed_barcode_amount"] = format(
                self.confirmed_barcode_amount, "f"
            )
        return payload

    def as_public_dict(self) -> dict[str, Any]:
        """Return safe job data for services, events and entities."""
        payload = self.as_storage_dict()
        payload["amount"] = float(self.amount)
        if self.confirmed_barcode_amount is not None:
            payload["confirmed_barcode_amount"] = float(
                self.confirmed_barcode_amount
            )
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
            raw_confirmed_barcode_amount = payload.get(
                "confirmed_barcode_amount"
            )
            raw_aliases = payload.get("accepted_aliases", ())
            if not isinstance(raw_aliases, (list, tuple)) or not all(
                isinstance(item, str) for item in raw_aliases
            ):
                raise ValueError
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
                confirmed_product_name=_optional_string(
                    payload.get("confirmed_product_name")
                ),
                confirmed_barcode_amount=(
                    Decimal(str(raw_confirmed_barcode_amount))
                    if raw_confirmed_barcode_amount is not None
                    else None
                ),
                accepted_aliases=tuple(
                    item.strip() for item in raw_aliases if item.strip()
                ),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as err:
            raise ValueError from err
        if (
            status
            not in {
                "searching",
                "ready",
                "manual_required",
                "confirming",
                "failed",
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
            or (
                job.confirmed_barcode_amount is not None
                and (
                    not job.confirmed_barcode_amount.is_finite()
                    or job.confirmed_barcode_amount <= 0
                )
            )
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

    def unresolved_jobs(self) -> tuple[ProductIdentificationJob, ...]:
        """Return every non-terminal job oldest-first."""
        return tuple(
            sorted(
                (
                    item
                    for item in self._records.values()
                    if item.status not in _TERMINAL_STATUSES
                ),
                key=lambda value: value.created_at,
            )
        )

    def pending_snapshot(self) -> list[dict[str, Any]]:
        """Return unresolved work oldest-first for dashboard presentation."""
        unresolved = self.unresolved_jobs()
        pending = unresolved[:25]
        queue_count = len(unresolved)
        return [
            {
                **item.as_public_dict(),
                "queue_position": index,
                "queue_count": queue_count,
                "is_queue_head": index == 1,
            }
            for index, item in enumerate(pending, start=1)
        ]

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
        journal: TransactionJournal,
        aliases: GrocyVoiceAliases,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._store = store
        self._journal = journal
        self._aliases = aliases
        self._semaphore = asyncio.Semaphore(3)
        self._confirmation_locks: defaultdict[str, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    def async_resume(self) -> None:
        """Resume jobs interrupted by an HA restart."""
        for job in self._store.searching_jobs():
            self._fire_update(job)
            self._schedule(job.job_id)
        self._entry.async_create_background_task(
            self._hass,
            self._async_recover_confirmations(),
            "Recover product-identification confirmations",
        )

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
            "queue": self.queue_summary(job.job_id),
        }

    def get(self, job_id: str) -> ProductIdentificationJob | None:
        """Return one immutable job for service orchestration."""
        return self._store.get(job_id)

    def queue_summary(self, job_id: str | None = None) -> dict[str, Any]:
        """Return compact queue metadata for scanner and tablet feedback."""
        pending = self._store.unresolved_jobs()
        position = next(
            (
                index
                for index, item in enumerate(pending, start=1)
                if item.job_id == job_id
            ),
            None,
        )
        return {
            "pending_count": len(pending),
            "position": position,
            "head_job_id": pending[0].job_id if pending else None,
        }

    async def async_begin_confirmation(
        self,
        job_id: str,
        product_name: str,
        accepted_aliases: tuple[str, ...],
        barcode_amount: Decimal,
    ) -> ProductIdentificationJob | None:
        """Persist human confirmation data before any catalogue or stock write."""
        async with self._confirmation_locks[job_id]:
            current = self._store.get(job_id)
            if current is None or current.status in _TERMINAL_STATUSES:
                return current
            if (
                current.status == "confirming"
                and current.confirmed_product_name is not None
                and (
                    current.confirmed_product_name.casefold()
                    != product_name.casefold()
                    or current.confirmed_barcode_amount != barcode_amount
                )
            ):
                return current
            updated = await self._store.async_update(
                job_id,
                expected_statuses=frozenset(
                    {
                        "searching",
                        "ready",
                        "manual_required",
                        "confirming",
                        "failed",
                    }
                ),
                status="confirming",
                stage="confirming",
                confirmed_product_name=product_name,
                confirmed_barcode_amount=barcode_amount,
                accepted_aliases=accepted_aliases,
                error_code=None,
                message="Confirming product and captured stock transaction",
            )
            if updated is not None:
                self._fire_update(updated)
            return updated

    async def async_recover_job(
        self, job_id: str
    ) -> dict[str, Any] | None:
        """Recover one confirmation from the durable transaction journal."""
        current = self._store.get(job_id)
        if current is None:
            return None
        prior = await self._journal.async_get(current.confirmation_request_id)
        if prior is None:
            return None
        result = prior["result"]
        if result.get("outcome") == "committed":
            product_name = (
                current.confirmed_product_name
                or _optional_string(result.get("product_name"))
                or current.candidate_name
                or f"Unknown item ({current.barcode})"
            )
            return await self.async_mark_committed(
                current.job_id,
                product_name,
                transaction=dict(result),
                catalogue=None,
                replayed=True,
            )
        if result.get("requires_reconciliation") or result.get("outcome") == "unknown":
            failed = await self.async_mark_failed(
                current.job_id,
                error_code="transaction_outcome_unknown",
                message=(
                    "The stock outcome is uncertain; reconcile it before taking "
                    "any further action on this review"
                ),
            )
            return {
                "response_version": 1,
                "success": False,
                "status": "failed",
                "stock_changed": False,
                "requires_reconciliation": True,
                "job": failed.as_public_dict() if failed is not None else None,
                "transaction": dict(result),
                "queue": self.queue_summary(current.job_id),
            }
        return None

    async def async_mark_committed(
        self,
        job_id: str,
        product_name: str,
        *,
        transaction: dict[str, Any],
        catalogue: dict[str, Any] | None,
        replayed: bool,
    ) -> dict[str, Any]:
        """Complete queue work before best-effort alias metadata is written."""
        current = self._store.get(job_id)
        if current is None:
            return {
                "response_version": 1,
                "success": False,
                "status": "not_found",
                "stock_changed": False,
            }
        updated = current
        if current.status != "completed":
            candidate = await self._store.async_update(
                job_id,
                expected_statuses=frozenset(
                    {
                        "searching",
                        "ready",
                        "manual_required",
                        "confirming",
                        "failed",
                    }
                ),
                status="completed",
                stage="completed",
                candidate_name=product_name,
                confirmed_product_name=product_name,
                error_code=None,
                message="Product confirmed and captured transaction committed",
            )
            if candidate is not None:
                updated = candidate
                self._fire_update(updated)

        raw_product_id = transaction.get("product_id")
        if isinstance(raw_product_id, int) and raw_product_id > 0:
            alias_results, warnings = await self._async_learn_aliases(
                updated,
                raw_product_id,
            )
        else:
            alias_results = []
            warnings = (
                ["Aliases were not learned because the product ID was unavailable."]
                if updated.accepted_aliases
                else []
            )
        return {
            "response_version": 1,
            "success": True,
            "status": "committed",
            "stock_changed": True,
            "replayed": replayed,
            "job": updated.as_public_dict(),
            "catalogue": catalogue,
            "transaction": transaction,
            "alias_results": alias_results,
            "warnings": warnings,
            "queue": self.queue_summary(job_id),
        }

    async def async_return_for_review(
        self,
        job_id: str,
        *,
        error_code: str,
        message: str,
    ) -> ProductIdentificationJob | None:
        """Return a safe pre-write rejection to the actionable queue."""
        current = self._store.get(job_id)
        status: IdentificationStatus = (
            "ready"
            if current is not None
            and (current.confirmed_product_name or current.candidate_name)
            else "manual_required"
        )
        updated = await self._store.async_update(
            job_id,
            expected_statuses=frozenset({"confirming"}),
            status=status,
            stage="confirmation" if status == "ready" else "manual_entry",
            error_code=error_code,
            message=message,
        )
        if updated is not None:
            self._fire_update(updated)
        return updated

    async def async_mark_failed(
        self,
        job_id: str,
        *,
        error_code: str,
        message: str,
    ) -> ProductIdentificationJob | None:
        """Keep uncertain work visible and fail closed."""
        updated = await self._store.async_update(
            job_id,
            expected_statuses=frozenset(
                {"searching", "ready", "manual_required", "confirming", "failed"}
            ),
            status="failed",
            stage="reconciliation",
            error_code=error_code,
            message=message,
        )
        if updated is not None:
            self._fire_update(updated)
        return updated

    async def _async_learn_aliases(
        self,
        job: ProductIdentificationJob,
        product_id: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Write optional voice aliases without changing the commit result."""
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        for alias in job.accepted_aliases:
            try:
                learned = await self._aliases.async_learn(alias, product_id)
            except Exception as err:  # Metadata failure must not relock stock work.
                warning = f"Could not learn alias {alias!r} ({type(err).__name__})"
                warnings.append(warning)
                _LOGGER.warning("%s for product %s", warning, product_id)
            else:
                results.append({"product_phrase": alias, "learned": learned})
        return results, warnings

    async def _async_recover_confirmations(self) -> None:
        """Resolve interrupted confirmations from journal evidence on startup."""
        for job in self._store.unresolved_jobs():
            recovered = await self.async_recover_job(job.job_id)
            if recovered is not None:
                continue
            if job.status == "confirming":
                updated = await self._store.async_update(
                    job.job_id,
                    expected_statuses=frozenset({"confirming"}),
                    status="ready" if job.confirmed_product_name else "manual_required",
                    stage=(
                        "confirmation"
                        if job.confirmed_product_name
                        else "manual_entry"
                    ),
                    error_code="confirmation_interrupted_before_stock_write",
                    message="Confirmation was interrupted safely; retry it",
                )
                if updated is not None:
                    self._fire_update(updated)

    async def async_override(
        self,
        job_id: str,
        *,
        product_name: str | None = None,
        product_aliases: tuple[str, ...] = (),
    ) -> ProductIdentificationJob | None:
        """Override AI with either a persisted catalogue result or manual entry."""
        current = self._store.get(job_id)
        if current is None or current.status in _TERMINAL_STATUSES:
            return current
        has_candidate = product_name is not None
        updated = await self._store.async_update(
            job_id,
            expected_statuses=frozenset(
                {"searching", "ready", "manual_required"}
            ),
            status="ready" if has_candidate else "manual_required",
            stage="catalogue_result" if has_candidate else "manual_entry",
            candidate_name=product_name,
            accepted_aliases=product_aliases if has_candidate else (),
            error_code=None,
            message=(
                "Catalogue suggestion ready for confirmation"
                if has_candidate
                else "Manual product entry requested"
            ),
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
                {"searching", "ready", "manual_required", "confirming", "failed"}
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

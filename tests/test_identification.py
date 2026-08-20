"""Tests for durable asynchronous product identification."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.grocy_stock_manager.identification import (
    ProductIdentificationJob,
    ProductIdentificationManager,
    ProductIdentificationStore,
    _extract_candidate_name,
    _identification_prompt,
)


def _job() -> ProductIdentificationJob:
    return ProductIdentificationJob(
        job_id="job-1",
        created_at=1_700_000_000,
        updated_at=1_700_000_001,
        status="searching",
        stage="ai_lookup",
        barcode="05000166157315",
        operation="add",
        amount=Decimal("4"),
        request_id="scanner:boot:4",
        location_id=None,
        location_name="Garage L3",
        quantity_unit_id=None,
        quantity_unit_name="Pack",
        source="garage_scanner",
        agent_id="conversation.openai_conversation",
        message="Searching with AI",
    )


def test_identification_job_round_trip_preserves_barcode_and_decimal() -> None:
    original = replace(_job(), confirmed_barcode_amount=Decimal("4"))
    restored = ProductIdentificationJob.from_storage_dict(
        original.as_storage_dict()
    )

    assert restored == original
    assert restored.barcode == "05000166157315"
    assert restored.amount == Decimal("4")
    assert restored.confirmed_barcode_amount == Decimal("4")


def test_confirmation_request_id_matches_legacy_idempotency_key() -> None:
    job = replace(_job(), request_id="garage:scanner:42:identify")

    assert job.confirmation_request_id == "garage:scanner:42:confirm"


def test_pending_snapshot_is_an_ordered_queue() -> None:
    store = object.__new__(ProductIdentificationStore)
    second = replace(_job(), job_id="job-2", created_at=1_700_000_010)
    store._records = {"job-2": second, "job-1": _job()}

    snapshot = store.pending_snapshot()

    assert [item["job_id"] for item in snapshot] == ["job-1", "job-2"]
    assert [item["queue_position"] for item in snapshot] == [1, 2]
    assert snapshot[0]["is_queue_head"] is True
    assert snapshot[1]["queue_count"] == 2


async def test_multiple_unknown_scans_are_saved_as_separate_queue_items() -> None:
    store = object.__new__(ProductIdentificationStore)
    store._records = {}
    store._lock = asyncio.Lock()
    store._async_save = AsyncMock()
    common = {
        "operation": "add",
        "amount": Decimal("1"),
        "location_id": None,
        "location_name": "Garage L2",
        "quantity_unit_id": None,
        "quantity_unit_name": "Pack",
        "source": "garage_scanner",
        "agent_id": "conversation.openai_conversation",
    }

    first, first_replayed = await store.async_create(
        barcode="11111111", request_id="scan-1:identify", **common
    )
    second, second_replayed = await store.async_create(
        barcode="22222222", request_id="scan-2:identify", **common
    )
    replay, replayed = await store.async_create(
        barcode="11111111", request_id="scan-1:identify", **common
    )

    assert first_replayed is False
    assert second_replayed is False
    assert replayed is True
    assert replay.job_id == first.job_id
    assert [item.job_id for item in store.unresolved_jobs()] == [
        first.job_id,
        second.job_id,
    ]


async def test_catalogue_candidate_is_queued_without_ai_search() -> None:
    store = object.__new__(ProductIdentificationStore)
    store._records = {}
    store._lock = asyncio.Lock()
    store._async_save = AsyncMock()

    job, replayed = await store.async_create(
        barcode="8720181948930",
        operation="add",
        amount=Decimal("1"),
        request_id="scan-catalogue:identify",
        location_id=None,
        location_name="Garage L2",
        quantity_unit_id=None,
        quantity_unit_name="Pack",
        source="garage_scanner",
        agent_id="catalogue",
        candidate_name="Domestos Bleach Foam Pine Boost 450ml",
        product_aliases=("bleach foam", "pine bleach"),
    )

    assert replayed is False
    assert job.status == "ready"
    assert job.stage == "catalogue_result"
    assert job.candidate_name == "Domestos Bleach Foam Pine Boost 450ml"
    assert job.accepted_aliases == ("bleach foam", "pine bleach")
    assert store.searching_jobs() == ()


def _manager(
    job: ProductIdentificationJob,
    *,
    journal_result: dict | None = None,
    alias_side_effect: object | None = None,
) -> tuple[ProductIdentificationManager, ProductIdentificationStore, AsyncMock]:
    store = object.__new__(ProductIdentificationStore)
    store._records = {job.job_id: job}
    store._lock = asyncio.Lock()
    store._async_save = AsyncMock()
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=Mock()))
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(async_update_listeners=Mock())
        )
    )
    journal = AsyncMock()
    journal.async_get.return_value = journal_result
    aliases = AsyncMock()
    if alias_side_effect is not None:
        aliases.async_learn.side_effect = alias_side_effect
    else:
        aliases.async_learn.return_value = True
    manager = ProductIdentificationManager(
        hass,
        entry,
        store,
        journal,
        aliases,
    )
    return manager, store, aliases


async def test_alias_failure_cannot_leave_committed_job_pending() -> None:
    job = replace(
        _job(),
        status="confirming",
        confirmed_product_name="Peanut butter",
        accepted_aliases=("peanut butter", "crunchy peanut butter"),
    )
    manager, store, aliases = _manager(
        job,
        alias_side_effect=[RuntimeError("metadata offline"), True],
    )

    response = await manager.async_mark_committed(
        job.job_id,
        "Peanut butter",
        transaction={
            "outcome": "committed",
            "product_id": 72,
            "product_name": "Peanut butter",
        },
        catalogue={"product_id": 72},
        replayed=False,
    )

    assert store.get(job.job_id).status == "completed"
    assert response["success"] is True
    assert response["status"] == "committed"
    assert response["queue"]["pending_count"] == 0
    assert len(response["warnings"]) == 1
    assert aliases.async_learn.await_count == 2


async def test_restart_recovers_committed_without_repeating_stock() -> None:
    job = replace(
        _job(),
        status="confirming",
        request_id="garage:scanner:42:identify",
        confirmed_product_name="Peanut butter",
        accepted_aliases=("peanut butter",),
    )
    manager, store, _aliases = _manager(
        job,
        journal_result={
            "result": {
                "outcome": "committed",
                "product_id": 72,
                "product_name": "Peanut butter",
            }
        },
    )

    response = await manager.async_recover_job(job.job_id)

    assert response is not None
    assert response["replayed"] is True
    assert store.get(job.job_id).status == "completed"


async def test_catalogue_override_persists_candidate_on_the_queue_item() -> None:
    manager, store, _aliases = _manager(_job())

    updated = await manager.async_override(
        "job-1",
        product_name="Crunchy peanut butter",
        product_aliases=("peanut butter", "crunchy peanut butter"),
    )

    assert updated is not None
    assert updated.status == "ready"
    assert updated.stage == "catalogue_result"
    assert updated.candidate_name == "Crunchy peanut butter"
    assert updated.accepted_aliases == (
        "peanut butter",
        "crunchy peanut butter",
    )
    assert store.pending_snapshot()[0]["candidate_name"] == (
        "Crunchy peanut butter"
    )


async def test_manual_override_clears_an_existing_candidate() -> None:
    manager, _store, _aliases = _manager(
        replace(_job(), status="ready", candidate_name="Wrong result")
    )

    updated = await manager.async_override("job-1")

    assert updated is not None
    assert updated.status == "manual_required"
    assert updated.stage == "manual_entry"
    assert updated.candidate_name is None
    assert updated.accepted_aliases == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "mystery"),
        ("operation", "transfer"),
        ("amount", "0"),
        ("confirmed_barcode_amount", "0"),
        ("request_id", ""),
    ],
)
def test_identification_job_rejects_invalid_storage(
    field: str, value: object
) -> None:
    payload = _job().as_storage_dict()
    payload[field] = value

    with pytest.raises(ValueError):
        ProductIdentificationJob.from_storage_dict(payload)


def test_extract_candidate_name_from_conversation_response() -> None:
    response = {
        "response": {
            "speech": {
                "plain": {
                    "speech": "cat food, Whiskas, fish favourites",
                }
            }
        }
    }

    assert _extract_candidate_name(response) == (
        "cat food, Whiskas, fish favourites"
    )


def test_extract_candidate_name_strips_markdown_link() -> None:
    response = {
        "response": {
            "speech": {
                "plain": {
                    "speech": "cat food, [Whiskas](https://example.test), fish",
                }
            }
        }
    }

    assert _extract_candidate_name(response) == "cat food, Whiskas, fish"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"response": {}},
        {"response": {"speech": {"plain": {"speech": "unknown item"}}}},
        {"response": {"speech": {"plain": {"speech": "Error: unavailable"}}}},
    ],
)
def test_extract_candidate_name_rejects_unusable_response(
    response: object,
) -> None:
    assert _extract_candidate_name(response) is None


def test_identification_prompt_requires_exact_barcode_match() -> None:
    prompt = _identification_prompt("5000166157315")

    assert "5000166157315" in prompt
    assert "exact barcode match" in prompt
    assert "unknown item" in prompt

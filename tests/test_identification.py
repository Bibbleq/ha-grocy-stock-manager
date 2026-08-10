"""Tests for durable asynchronous product identification."""

from decimal import Decimal

import pytest

from custom_components.grocy_stock_manager.identification import (
    ProductIdentificationJob,
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
    restored = ProductIdentificationJob.from_storage_dict(
        _job().as_storage_dict()
    )

    assert restored == _job()
    assert restored.barcode == "05000166157315"
    assert restored.amount == Decimal("4")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "mystery"),
        ("operation", "transfer"),
        ("amount", "0"),
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

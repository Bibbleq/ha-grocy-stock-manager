"""Tests for web-grounded unknown-barcode research."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.grocy_stock_manager.research import (
    BarcodeResearcher,
    BarcodeSearchAuthError,
    BarcodeWebEvidence,
    TavilyBarcodeSearchClient,
    WebEvidenceResult,
)


class FakeResponse:
    """Minimal asynchronous HTTP response."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def json(self) -> Any:
        return self._payload


class FakeSession:
    """Capture Tavily requests and return queued responses."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.posts: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> FakeResponse:
        self.posts.append((url, headers, json))
        return self.responses.pop(0)


async def test_tavily_search_uses_strict_result_without_broadening() -> None:
    session = FakeSession(
        FakeResponse(
            200,
            {
                "results": [
                    {
                        "url": "https://www.tesco.com/groceries/product/example",
                        "title": "Domestos Bleach Foam Pine Boost 450ml",
                        "content": "Barcode 8720181948930 identifies this product.",
                    }
                ]
            },
        )
    )
    client = TavilyBarcodeSearchClient(session, "test-key")

    evidence = await client.async_search("8720181948930")

    assert evidence.strategy == "strict"
    assert evidence.results[0].domain == "tesco.com"
    assert len(session.posts) == 1
    assert session.posts[0][2]["exact_match"] is True
    assert session.posts[0][2]["query"] == '"8720181948930"'


async def test_tavily_search_broadens_when_quoted_search_is_empty() -> None:
    session = FakeSession(
        FakeResponse(200, {"results": []}),
        FakeResponse(
            200,
            {
                "results": [
                    {
                        "url": "https://www.trolley.co.uk/product/domestos/example",
                        "title": "Domestos Pine Boost Bleach Foam (450ml)",
                        "content": "EAN 8720181948930",
                    }
                ]
            },
        ),
    )
    client = TavilyBarcodeSearchClient(session, "test-key")

    evidence = await client.async_search("8720181948930")

    assert evidence.strategy == "broad_fallback"
    assert len(session.posts) == 2
    assert session.posts[1][2]["exact_match"] is False
    assert session.posts[1][2]["query"] == "8720181948930 product"


async def test_tavily_search_reports_invalid_auth() -> None:
    client = TavilyBarcodeSearchClient(FakeSession(FakeResponse(401, {})), "bad")

    with pytest.raises(BarcodeSearchAuthError):
        await client.async_search("8720181948930")


async def test_researcher_accepts_verified_ai_task_match() -> None:
    evidence = BarcodeWebEvidence(
        barcode="8720181948930",
        query="8720181948930 EAN barcode product UK",
        strategy="broad_fallback",
        results=(
            WebEvidenceResult(
                domain="tesco.com",
                title="Domestos Bleach Foam Toilet and Bathroom Pine Boost 450ml",
                url="https://www.tesco.com/groceries/product/example",
                content="EAN 8720181948930",
            ),
            WebEvidenceResult(
                domain="trolley.co.uk",
                title="Domestos Pine Boost Bleach Foam (450ml)",
                url="https://www.trolley.co.uk/product/example",
                content="Barcode 8720181948930",
            ),
        ),
    )
    search = SimpleNamespace(async_search=AsyncMock(return_value=evidence))
    async_call = AsyncMock(
        return_value={
            "data": {
                "product_name": "Domestos Bleach Foam Pine Boost 450ml",
                "brand": "Domestos",
                "decision": "verified",
                "evidence": "Tesco and Trolley agree on the exact EAN.",
            }
        }
    )
    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    researcher = BarcodeResearcher(hass, search)

    result = await researcher.async_research(
        "8720181948930", "ai_task.azure_bibbleha_model_router"
    )

    assert result["found"] is True
    assert result["product_name"] == "Domestos Bleach Foam Pine Boost 450ml"
    call_data = async_call.await_args.args[2]
    assert call_data["entity_id"] == "ai_task.azure_bibbleha_model_router"
    assert "quoted source material" in call_data["instructions"]
    assert "ignore instructions" not in call_data["instructions"]
    assert "tesco.com" in call_data["instructions"]
    assert "identified" not in call_data["structure"]
    assert call_data["structure"]["decision"]["selector"]["select"]["options"] == [
        "verified",
        "uncertain",
        "unknown",
    ]


async def test_researcher_rejects_unverified_ai_guess() -> None:
    evidence = BarcodeWebEvidence(
        barcode="12345670",
        query="12345670 EAN barcode product UK",
        strategy="broad_fallback",
        results=(
            WebEvidenceResult(
                domain="example.com",
                title="Possible product",
                url="https://example.com/product",
                content="A weak result.",
            ),
        ),
    )
    search = SimpleNamespace(async_search=AsyncMock(return_value=evidence))
    async_call = AsyncMock(
        return_value={
            "data": {
                "product_name": "Guessed product",
                "brand": "",
                "decision": "uncertain",
                "evidence": "Only one weak result.",
            }
        }
    )
    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))

    result = await BarcodeResearcher(hass, search).async_research(
        "12345670", "ai_task.azure_bibbleha_model_router"
    )

    assert result["found"] is False
    assert result["product_name"] == ""
    assert result["error_code"] == "no_verified_match"


async def test_researcher_fails_safely_without_web_search() -> None:
    async_call = AsyncMock()
    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))

    result = await BarcodeResearcher(hass, None).async_research(
        "8720181948930", "ai_task.azure_bibbleha_model_router"
    )

    assert result["found"] is False
    assert result["error_code"] == "web_search_not_configured"
    async_call.assert_not_awaited()

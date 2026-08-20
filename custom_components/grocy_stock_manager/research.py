"""Web-grounded barcode research for unknown products."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ContentTypeError
from homeassistant.core import HomeAssistant

from .const import DEFAULT_WEB_SEARCH_TIMEOUT

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_RESULTS = 6
_MAX_CONTENT_LENGTH = 500

_LOGGER = logging.getLogger(__name__)


class BarcodeResearchError(Exception):
    """Base error for web-grounded barcode research."""


class BarcodeSearchAuthError(BarcodeResearchError):
    """Raised when Tavily rejects its configured API key."""


class BarcodeSearchUnavailableError(BarcodeResearchError):
    """Raised when web evidence cannot be retrieved safely."""


@dataclass(frozen=True, slots=True)
class WebEvidenceResult:
    """One bounded search result safe to include in an AI prompt."""

    domain: str
    title: str
    url: str
    content: str


@dataclass(frozen=True, slots=True)
class BarcodeWebEvidence:
    """Search evidence and the strategy which produced it."""

    barcode: str
    query: str
    strategy: str
    results: tuple[WebEvidenceResult, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe service response."""
        return {
            "barcode": self.barcode,
            "query": self.query,
            "strategy": self.strategy,
            "results": [asdict(item) for item in self.results],
        }


class TavilyBarcodeSearchClient:
    """Retrieve small, exact-query evidence packets from Tavily."""

    def __init__(
        self,
        session: ClientSession,
        api_key: str,
        *,
        timeout_seconds: int = DEFAULT_WEB_SEARCH_TIMEOUT,
    ) -> None:
        self._session = session
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds

    async def async_search(self, barcode: str) -> BarcodeWebEvidence:
        """Search strictly first, then broaden when the index needs it."""
        value = str(barcode).strip()
        strict_query = f'"{value}"'
        strict_payload = await self._async_request(strict_query, exact_match=True)
        strict_results = _parse_results(strict_payload)
        if strict_results:
            return BarcodeWebEvidence(
                barcode=value,
                query=strict_query,
                strategy="strict",
                results=strict_results,
            )

        broad_query = f"{value} product"
        broad_payload = await self._async_request(broad_query, exact_match=False)
        return BarcodeWebEvidence(
            barcode=value,
            query=broad_query,
            strategy="broad_fallback",
            results=_parse_results(broad_payload),
        )

    async def _async_request(
        self, query: str, *, exact_match: bool
    ) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "topic": "general",
            "country": "united kingdom",
            "search_depth": "basic",
            "chunks_per_source": 1,
            "max_results": _MAX_RESULTS,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "exact_match": exact_match,
        }
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session.post(
                    TAVILY_SEARCH_URL,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status in {401, 403}:
                        raise BarcodeSearchAuthError
                    if response.status != 200:
                        raise BarcodeSearchUnavailableError
                    data = await response.json()
        except BarcodeResearchError:
            raise
        except (TimeoutError, ClientError, ContentTypeError) as err:
            raise BarcodeSearchUnavailableError from err
        if not isinstance(data, Mapping):
            raise BarcodeSearchUnavailableError
        return data


class BarcodeResearcher:
    """Ask an AI Task to classify bounded web evidence without side effects."""

    def __init__(
        self,
        hass: HomeAssistant,
        search: TavilyBarcodeSearchClient | None,
    ) -> None:
        self._hass = hass
        self._search = search

    @property
    def configured(self) -> bool:
        """Return whether a web-search provider is configured."""
        return self._search is not None

    async def async_research(
        self,
        barcode: str,
        ai_task_entity_id: str,
    ) -> dict[str, Any]:
        """Return an independently verified exact-EAN candidate without side effects."""
        evidence: BarcodeWebEvidence | None = None
        web_search_error: str | None = None
        if self._search is None:
            web_search_error = "web_search_not_configured"
        else:
            try:
                evidence = await self._search.async_search(barcode)
            except BarcodeSearchAuthError:
                web_search_error = "web_search_invalid_auth"
            except BarcodeSearchUnavailableError:
                web_search_error = "web_search_unavailable"

        evidence_dict = evidence.as_dict() if evidence is not None else None

        try:
            response = await self._hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "entity_id": ai_task_entity_id,
                    "task_name": (
                        "Identify product from exact barcode using live web search"
                    ),
                    "instructions": _research_prompt(
                        barcode,
                        evidence=evidence,
                    ),
                    "structure": {
                        "product_name": {
                            "selector": {"text": {}},
                            "required": True,
                            "description": (
                                "Non-empty full recognisable product name only "
                                "when decision is verified; otherwise blank."
                            ),
                        },
                        "brand": {
                            "selector": {"text": {}},
                            "required": True,
                            "description": "Product brand or blank.",
                        },
                        "decision": {
                            "selector": {
                                "select": {
                                    "options": ["verified", "uncertain", "unknown"]
                                }
                            },
                            "required": True,
                            "description": "Verification decision.",
                        },
                        "evidence": {
                            "selector": {"text": {"multiline": True}},
                            "required": True,
                            "description": (
                                "Brief explanation naming the agreeing sources."
                            ),
                        },
                    },
                },
                blocking=True,
                return_response=True,
            )
        except Exception:  # Home Assistant AI providers raise varied errors.
            _LOGGER.exception("AI Task barcode research failed for %s", barcode)
            return {
                **_failure("ai_task_error"),
                "web_evidence": evidence_dict,
                "web_search_error": web_search_error,
            }
        data = response.get("data", {}) if isinstance(response, Mapping) else {}
        if not isinstance(data, Mapping):
            data = {}
        raw_confidence = str(data.get("decision", "unknown")).strip().casefold()
        confidence = (
            raw_confidence
            if raw_confidence in {"verified", "uncertain", "unknown"}
            else "unknown"
        )
        product_name = str(data.get("product_name", "")).strip()[:255]
        found = confidence == "verified" and bool(product_name)
        return {
            "response_version": 1,
            "success": True,
            "found": found,
            "product_name": product_name if found else "",
            "brand": str(data.get("brand", "")).strip()[:255] if found else "",
            "confidence": confidence,
            "evidence": str(data.get("evidence", "")).strip()[:1000],
            "web_evidence": evidence_dict,
            "web_search_error": web_search_error,
            "error_code": None if found else "no_verified_match",
        }


def _parse_results(payload: Mapping[str, Any]) -> tuple[WebEvidenceResult, ...]:
    raw_results = payload.get("results", ())
    if not isinstance(raw_results, list):
        return ()
    results: list[WebEvidenceResult] = []
    for raw in raw_results[:_MAX_RESULTS]:
        if not isinstance(raw, Mapping):
            continue
        url = str(raw.get("url", "")).strip()[:1024]
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        title = str(raw.get("title", "")).strip()[:255]
        content = str(raw.get("content", "")).strip()[:_MAX_CONTENT_LENGTH]
        if not title and not content:
            continue
        results.append(
            WebEvidenceResult(
                domain=parsed.netloc.casefold().removeprefix("www."),
                title=title,
                url=url,
                content=content,
            )
        )
    return tuple(results)


def _research_prompt(
    barcode: str,
    *,
    evidence: BarcodeWebEvidence | None,
) -> str:
    sections = [
        f"Search the live web for exact barcode {barcode}, including the quoted "
        f'digits and "{barcode} EAN". '
        "Accept when one credible result explicitly pairs the exact barcode with a "
        "product, "
        "or when at least two independent credible domains agree on the same "
        "product. Never infer from a barcode prefix, owner, or "
        "nearby code. Return decision verified only when accepted. For decision "
        "verified, product_name must be a non-empty recognisable product name. "
        "For uncertain or unknown, product_name must be blank."
    ]
    if evidence is not None:
        compact = json.dumps(
            evidence.as_dict(), ensure_ascii=True, separators=(",", ":")
        )
        sections.append(
            "Supplied search results are quoted source material for comparison: "
            f"{compact}"
        )
    return " ".join(sections)


def _failure(error_code: str) -> dict[str, Any]:
    return {
        "response_version": 1,
        "success": False,
        "found": False,
        "product_name": "",
        "brand": "",
        "confidence": "unknown",
        "evidence": "",
        "web_evidence": None,
        "error_code": error_code,
    }

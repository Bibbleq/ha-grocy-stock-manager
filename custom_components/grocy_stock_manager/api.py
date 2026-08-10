"""Small asynchronous client for the Grocy API surface used by this project."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp import ClientError, ClientSession, ContentTypeError

from .const import DEFAULT_REQUEST_TIMEOUT


class GrocyApiError(Exception):
    """Base exception for Grocy API errors."""


class GrocyCannotConnectError(GrocyApiError):
    """Raised when Grocy cannot be reached reliably."""


class GrocyInvalidAuthError(GrocyApiError):
    """Raised when Grocy rejects the supplied API key."""


class GrocyInvalidResponseError(GrocyApiError):
    """Raised when Grocy returns an unexpected response."""


class GrocyNotFoundError(GrocyApiError):
    """Raised when a requested Grocy object does not exist."""


class GrocyMutationOutcomeUnknownError(GrocyApiError):
    """Raised when a write may have reached Grocy but no response was received."""


class GrocyAmbiguousProductError(GrocyApiError):
    """Raised when an exact product-name lookup is not unique."""


def _created_object_id(payload: Any) -> int:
    """Return a validated ID from Grocy's generic object-create response."""
    if not isinstance(payload, dict):
        raise GrocyMutationOutcomeUnknownError
    try:
        object_id = int(payload["created_object_id"])
    except (KeyError, TypeError, ValueError) as err:
        raise GrocyMutationOutcomeUnknownError from err
    if object_id < 1:
        raise GrocyMutationOutcomeUnknownError
    return object_id


def normalise_base_url(value: str) -> str:
    """Return a canonical Grocy base URL without a trailing /api segment."""
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("A valid HTTP or HTTPS Grocy URL is required")

    path = parsed.path.rstrip("/")
    if path.endswith("/api"):
        path = path[:-4]

    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


class GrocyApiClient:
    """Interact with the small Grocy API surface needed by the integration."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        api_key: str,
        *,
        request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._base_url = normalise_base_url(base_url)
        self._api_key = api_key
        self._request_timeout = request_timeout

    @property
    def base_url(self) -> str:
        """Return the canonical Grocy base URL."""
        return self._base_url

    async def _async_get_json(
        self,
        path: str,
        *,
        params: Sequence[tuple[str, str]] | None = None,
        not_found_statuses: frozenset[int] = frozenset(),
    ) -> Any:
        """Return decoded JSON from a Grocy GET endpoint."""
        url = f"{self._base_url}/api/{path.lstrip('/')}"
        try:
            async with asyncio.timeout(self._request_timeout):
                async with self._session.get(
                    url,
                    headers={"GROCY-API-KEY": self._api_key},
                    params=params,
                ) as response:
                    if response.status in {401, 403}:
                        raise GrocyInvalidAuthError
                    if response.status in not_found_statuses:
                        raise GrocyNotFoundError
                    if response.status >= 400:
                        raise GrocyApiError(f"Grocy returned HTTP {response.status}")

                    try:
                        payload = await response.json(content_type=None)
                    except (ContentTypeError, ValueError) as err:
                        raise GrocyInvalidResponseError from err
        except GrocyApiError:
            raise
        except (TimeoutError, ClientError) as err:
            raise GrocyCannotConnectError from err

        return payload

    async def _async_post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        """POST JSON to Grocy, preserving uncertainty after transport failure."""
        url = f"{self._base_url}/api/{path.lstrip('/')}"
        try:
            async with asyncio.timeout(self._request_timeout):
                async with self._session.post(
                    url,
                    headers={"GROCY-API-KEY": self._api_key},
                    json=dict(payload),
                ) as response:
                    if response.status in {401, 403}:
                        raise GrocyInvalidAuthError
                    if response.status >= 400:
                        raise GrocyApiError(f"Grocy returned HTTP {response.status}")

                    try:
                        result = await response.json(content_type=None)
                    except (ContentTypeError, ValueError) as err:
                        # A successful HTTP status means Grocy may already have
                        # committed the write even when its body is unreadable.
                        raise GrocyMutationOutcomeUnknownError from err
        except GrocyApiError:
            raise
        except (TimeoutError, ClientError) as err:
            raise GrocyMutationOutcomeUnknownError from err

        return result

    async def _async_put(self, path: str, payload: Mapping[str, Any]) -> None:
        """PUT JSON to Grocy, preserving uncertainty after transport failure."""
        url = f"{self._base_url}/api/{path.lstrip('/')}"
        try:
            async with asyncio.timeout(self._request_timeout):
                async with self._session.put(
                    url,
                    headers={"GROCY-API-KEY": self._api_key},
                    json=dict(payload),
                ) as response:
                    if response.status in {401, 403}:
                        raise GrocyInvalidAuthError
                    if response.status >= 400:
                        raise GrocyApiError(f"Grocy returned HTTP {response.status}")
        except GrocyApiError:
            raise
        except (TimeoutError, ClientError) as err:
            raise GrocyMutationOutcomeUnknownError from err

    async def _async_post_no_response(
        self, path: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        """POST JSON to an endpoint whose successful response has no body."""
        url = f"{self._base_url}/api/{path.lstrip('/')}"
        try:
            async with asyncio.timeout(self._request_timeout):
                async with self._session.post(
                    url,
                    headers={"GROCY-API-KEY": self._api_key},
                    json=dict(payload or {}),
                ) as response:
                    if response.status in {401, 403}:
                        raise GrocyInvalidAuthError
                    if response.status >= 400:
                        raise GrocyApiError(f"Grocy returned HTTP {response.status}")
        except GrocyApiError:
            raise
        except (TimeoutError, ClientError) as err:
            # The merge endpoint is transactional, but a transport failure can
            # occur after Grocy committed it. Callers must verify by reading.
            raise GrocyMutationOutcomeUnknownError from err

    async def async_get_system_info(self) -> Mapping[str, Any]:
        """Return Grocy system information and validate connectivity/auth."""
        payload = await self._async_get_json("system/info")

        if not isinstance(payload, dict):
            raise GrocyInvalidResponseError

        return payload

    async def async_get_product_by_barcode(self, barcode: str) -> Mapping[str, Any]:
        """Return product details for an exact barcode."""
        encoded_barcode = quote(barcode, safe="")
        payload = await self._async_get_json(
            f"stock/products/by-barcode/{encoded_barcode}",
            not_found_statuses=frozenset({400, 404}),
        )
        if not isinstance(payload, dict):
            raise GrocyInvalidResponseError
        return payload

    async def async_get_product_by_id(self, product_id: int) -> Mapping[str, Any]:
        """Return product details for an exact Grocy product id."""
        payload = await self._async_get_json(
            f"stock/products/{product_id}",
            not_found_statuses=frozenset({400, 404}),
        )
        if not isinstance(payload, dict):
            raise GrocyInvalidResponseError
        return payload

    async def async_get_product_by_name(self, product_name: str) -> Mapping[str, Any]:
        """Return product details for one exact Grocy product name."""
        payload = await self._async_get_json(
            "objects/products",
            params=(("query[]", f"name={product_name}"), ("limit", "2")),
        )
        if not isinstance(payload, list):
            raise GrocyInvalidResponseError
        if not payload:
            raise GrocyNotFoundError
        if len(payload) > 1:
            raise GrocyAmbiguousProductError

        from .models import parse_product_summary_id

        return await self.async_get_product_by_id(parse_product_summary_id(payload[0]))

    async def async_get_products(self) -> list[Mapping[str, Any]]:
        """Return all configured Grocy product summaries."""
        payload = await self._async_get_json("objects/products")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrocyInvalidResponseError
        return payload

    async def async_get_product_userfields(
        self, product_id: int
    ) -> Mapping[str, Any]:
        """Return the configured userfield values for one product."""
        payload = await self._async_get_json(
            f"userfields/products/{product_id}",
            not_found_statuses=frozenset({404}),
        )
        if not isinstance(payload, dict):
            raise GrocyInvalidResponseError
        return payload

    async def async_set_product_userfield(
        self,
        product_id: int,
        field_name: str,
        value: str | None,
    ) -> None:
        """Set one product userfield without touching any other field."""
        await self._async_put(
            f"userfields/products/{product_id}",
            {field_name: value},
        )

    async def async_get_product_stock_locations(
        self, product_id: int
    ) -> list[Mapping[str, Any]]:
        """Return locations where the product currently has stock."""
        payload = await self._async_get_json(
            f"stock/products/{product_id}/locations",
            not_found_statuses=frozenset({400, 404}),
        )
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrocyInvalidResponseError
        return payload

    async def async_get_product_stock_entries(
        self, product_id: int
    ) -> list[Mapping[str, Any]]:
        """Return the product's current Grocy stock entries."""
        payload = await self._async_get_json(
            f"stock/products/{product_id}/entries",
            not_found_statuses=frozenset({400, 404}),
        )
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrocyInvalidResponseError
        return payload

    async def async_get_locations(self) -> list[Mapping[str, Any]]:
        """Return all configured Grocy locations."""
        payload = await self._async_get_json("objects/locations")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrocyInvalidResponseError
        return payload

    async def async_get_quantity_units(self) -> list[Mapping[str, Any]]:
        """Return all configured Grocy quantity units."""
        payload = await self._async_get_json("objects/quantity_units")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrocyInvalidResponseError
        return payload

    async def async_create_product(
        self,
        name: str,
        *,
        location_id: int,
        quantity_unit_id: int,
    ) -> int:
        """Create the minimum deterministic Grocy product master data."""
        payload = await self._async_post_json(
            "objects/products",
            {
                "name": name,
                "location_id": location_id,
                "qu_id_purchase": quantity_unit_id,
                "qu_id_stock": quantity_unit_id,
            },
        )
        return _created_object_id(payload)

    async def async_create_product_barcode(
        self,
        product_id: int,
        barcode: str,
        *,
        quantity_unit_id: int,
    ) -> int:
        """Attach one exact barcode representing one stock unit to a product."""
        payload = await self._async_post_json(
            "objects/product_barcodes",
            {
                "product_id": product_id,
                "barcode": barcode,
                "qu_id": quantity_unit_id,
                "amount": 1,
            },
        )
        return _created_object_id(payload)

    async def async_update_product(
        self, product_id: int, changes: Mapping[str, Any]
    ) -> None:
        """Update only the supplied product master-data fields."""
        await self._async_put(f"objects/products/{product_id}", changes)

    async def async_merge_products(
        self, product_id_to_keep: int, product_id_to_remove: int
    ) -> None:
        """Use Grocy's transactional native product merge operation."""
        await self._async_post_no_response(
            f"stock/products/{product_id_to_keep}/merge/{product_id_to_remove}"
        )

    async def async_add_product(
        self, product_id: int, *, amount: str, location_id: int
    ) -> list[Mapping[str, Any]]:
        """Add an exact stock-unit amount at one explicit location."""
        payload = await self._async_post_json(
            f"stock/products/{product_id}/add",
            {
                "amount": amount,
                "location_id": location_id,
                "transaction_type": "purchase",
            },
        )
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrocyMutationOutcomeUnknownError
        return payload

    async def async_consume_product(
        self, product_id: int, *, amount: str, location_id: int
    ) -> list[Mapping[str, Any]]:
        """Consume an exact stock-unit amount from one explicit location."""
        payload = await self._async_post_json(
            f"stock/products/{product_id}/consume",
            {
                "amount": amount,
                "location_id": location_id,
                "spoiled": False,
                "transaction_type": "consume",
            },
        )
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrocyMutationOutcomeUnknownError
        return payload

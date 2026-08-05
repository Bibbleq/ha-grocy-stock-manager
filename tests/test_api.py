"""Tests for the direct Grocy API client."""

from typing import Any

import pytest

from custom_components.grocy_stock_manager.api import (
    GrocyAmbiguousProductError,
    GrocyApiClient,
    GrocyInvalidAuthError,
    GrocyInvalidResponseError,
    GrocyNotFoundError,
    normalise_base_url,
)


class FakeResponse:
    """Minimal asynchronous response used to isolate client behaviour."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        """Enter the response context."""
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """Exit the response context."""

    async def json(self, *, content_type: str | None = None) -> Any:
        """Return the configured JSON payload."""
        return self._payload


class FakeSession:
    """Capture requests and return configured responses in order."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.url: str | None = None
        self.headers: dict[str, str] | None = None
        self.params: tuple[tuple[str, str], ...] | None = None
        self.requests: list[
            tuple[str, dict[str, str], tuple[tuple[str, str], ...] | None]
        ] = []
        self.posts: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: tuple[tuple[str, str], ...] | None = None,
    ) -> FakeResponse:
        """Capture a GET and return the fake response context."""
        self.url = url
        self.headers = headers
        self.params = params
        self.requests.append((url, headers, params))
        return self.responses.pop(0)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> FakeResponse:
        """Capture a POST and return the fake response context."""
        self.posts.append((url, headers, json))
        return self.responses.pop(0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://grocy.local:9192/", "http://grocy.local:9192"),
        ("HTTPS://grocy.example/api", "https://grocy.example"),
        (
            "https://example.test/grocy/api/",
            "https://example.test/grocy",
        ),
    ],
)
def test_normalise_base_url(value: str, expected: str) -> None:
    """Grocy URLs are canonicalised consistently."""
    assert normalise_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "grocy.local", "ftp://grocy.local", "https://grocy.local?key=value"],
)
def test_normalise_base_url_rejects_invalid_values(value: str) -> None:
    """Invalid and unsafe base URLs are rejected."""
    with pytest.raises(ValueError):
        normalise_base_url(value)


async def test_get_system_info() -> None:
    """The client sends the API key and returns Grocy system information."""
    url = "http://grocy.local:9192/api/system/info"
    payload = {"grocy_version": {"Version": "4.6.0"}}
    session = FakeSession(FakeResponse(200, payload))
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    assert await client.async_get_system_info() == payload
    assert session.url == url
    assert session.headers == {"GROCY-API-KEY": "secret"}
    assert session.params is None


async def test_get_system_info_rejects_invalid_auth() -> None:
    """Authentication failures use a distinct exception."""
    session = FakeSession(FakeResponse(401, {}))
    client = GrocyApiClient(session, "http://grocy.local:9192", "bad")

    with pytest.raises(GrocyInvalidAuthError):
        await client.async_get_system_info()


async def test_get_system_info_rejects_non_object_payload() -> None:
    """The system-info response must be a JSON object."""
    session = FakeSession(FakeResponse(200, ["unexpected"]))
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    with pytest.raises(GrocyInvalidResponseError):
        await client.async_get_system_info()


async def test_get_product_by_barcode_preserves_and_encodes_value() -> None:
    """Barcode lookup preserves leading zeroes and safely encodes the path."""
    payload = {"product": {"id": "1", "name": "Synthetic product"}}
    session = FakeSession(FakeResponse(200, payload))
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    assert await client.async_get_product_by_barcode("0012/34") == payload
    assert session.url == (
        "http://grocy.local:9192/api/stock/products/by-barcode/0012%2F34"
    )


async def test_get_product_by_barcode_reports_unknown() -> None:
    """Grocy's HTTP 400 unknown-barcode response has a distinct exception."""
    session = FakeSession(FakeResponse(400, {"error_message": "Unknown barcode"}))
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    with pytest.raises(GrocyNotFoundError):
        await client.async_get_product_by_barcode("0000000000000")


async def test_get_product_by_name_uses_exact_server_filter() -> None:
    """Exact product-name lookup resolves the returned product id."""
    details = {"product": {"id": "7", "name": "Synthetic product"}}
    session = FakeSession(
        FakeResponse(200, [{"id": "7", "name": "Synthetic product"}]),
        FakeResponse(200, details),
    )
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    assert await client.async_get_product_by_name("Synthetic product") == details
    assert session.requests[0][0] == "http://grocy.local:9192/api/objects/products"
    assert session.requests[0][2] == (
        ("query[]", "name=Synthetic product"),
        ("limit", "2"),
    )
    assert session.requests[1][0] == (
        "http://grocy.local:9192/api/stock/products/7"
    )


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], GrocyNotFoundError),
        ([{"id": "1"}, {"id": "2"}], GrocyAmbiguousProductError),
    ],
)
async def test_get_product_by_name_rejects_non_unique_results(
    payload: list[dict[str, str]], error: type[Exception]
) -> None:
    """An exact name must resolve to one and only one Grocy product."""
    session = FakeSession(FakeResponse(200, payload))
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    with pytest.raises(error):
        await client.async_get_product_by_name("Synthetic product")


async def test_get_product_stock_locations_requires_an_array() -> None:
    """Unexpected stock-location response shapes fail closed."""
    session = FakeSession(FakeResponse(200, {"location_id": "1"}))
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    with pytest.raises(GrocyInvalidResponseError):
        await client.async_get_product_stock_locations(1)


async def test_read_only_location_and_stock_entry_routes() -> None:
    """The client exposes the remaining Phase 2 read endpoints."""
    locations = [{"id": "12", "name": "Garage Synthetic"}]
    entries = [
        {
            "id": "4",
            "stock_id": "synthetic-stock-id",
            "product_id": "1",
            "location_id": "12",
            "amount": "3",
        }
    ]
    session = FakeSession(
        FakeResponse(200, locations),
        FakeResponse(200, entries),
    )
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    assert await client.async_get_locations() == locations
    assert await client.async_get_product_stock_entries(1) == entries
    assert session.requests[0][0].endswith("/api/objects/locations")
    assert session.requests[1][0].endswith("/api/stock/products/1/entries")


async def test_add_and_consume_use_explicit_location_payloads() -> None:
    """Write helpers send exact stock-unit strings and never leave location implicit."""
    session = FakeSession(
        FakeResponse(200, [{"id": "101"}]),
        FakeResponse(200, [{"id": "102"}]),
    )
    client = GrocyApiClient(session, "http://grocy.local:9192", "secret")

    assert await client.async_add_product(1, amount="2.5", location_id=12) == [
        {"id": "101"}
    ]
    assert await client.async_consume_product(
        1, amount="1", location_id=12
    ) == [{"id": "102"}]
    assert session.posts[0][0].endswith("/api/stock/products/1/add")
    assert session.posts[0][2] == {
        "amount": "2.5",
        "location_id": 12,
        "transaction_type": "purchase",
    }
    assert session.posts[1][0].endswith("/api/stock/products/1/consume")
    assert session.posts[1][2] == {
        "amount": "1",
        "location_id": 12,
        "spoiled": False,
        "transaction_type": "consume",
    }

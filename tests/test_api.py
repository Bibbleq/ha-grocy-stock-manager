"""Tests for the direct Grocy API client."""

from typing import Any

import pytest

from custom_components.grocy_stock_manager.api import (
    GrocyApiClient,
    GrocyInvalidAuthError,
    GrocyInvalidResponseError,
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
    """Capture requests and return one configured response."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.url: str | None = None
        self.headers: dict[str, str] | None = None

    def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        """Capture a GET and return the fake response context."""
        self.url = url
        self.headers = headers
        return self.response


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

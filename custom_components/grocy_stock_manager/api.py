"""Small asynchronous client for the Grocy API surface used by this project."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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

    async def async_get_system_info(self) -> Mapping[str, Any]:
        """Return Grocy system information and validate connectivity/auth."""
        url = f"{self._base_url}/api/system/info"

        try:
            async with asyncio.timeout(self._request_timeout):
                async with self._session.get(
                    url,
                    headers={"GROCY-API-KEY": self._api_key},
                ) as response:
                    if response.status in {401, 403}:
                        raise GrocyInvalidAuthError
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

        if not isinstance(payload, dict):
            raise GrocyInvalidResponseError

        return payload

"""Home Assistant actions for Grocy Stock Manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import (
    GrocyAmbiguousProductError,
    GrocyApiError,
    GrocyCannotConnectError,
    GrocyInvalidAuthError,
    GrocyInvalidResponseError,
    GrocyNotFoundError,
)
from .const import (
    ATTR_BARCODE,
    ATTR_PRODUCT_ID,
    ATTR_PRODUCT_NAME,
    DOMAIN,
    SERVICE_LOOKUP,
)

if TYPE_CHECKING:
    from . import GrocyStockManagerConfigEntry


def _non_empty_string(value: Any) -> str:
    """Normalise and validate a non-empty action string."""
    normalised = cv.string(value).strip()
    if not normalised:
        raise vol.Invalid("value must not be empty")
    return normalised


def _exactly_one_identifier(data: dict[str, Any]) -> dict[str, Any]:
    """Require exactly one supported product identifier."""
    identifier_count = sum(
        key in data for key in (ATTR_BARCODE, ATTR_PRODUCT_ID, ATTR_PRODUCT_NAME)
    )
    if identifier_count != 1:
        raise vol.Invalid(
            "exactly one of barcode, product_id, or product_name is required"
        )
    return data


LOOKUP_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_BARCODE): vol.All(_non_empty_string, vol.Length(max=128)),
            vol.Optional(ATTR_PRODUCT_ID): vol.All(
                vol.Coerce(int), vol.Range(min=1)
            ),
            vol.Optional(ATTR_PRODUCT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
        }
    ),
    _exactly_one_identifier,
)


def _identifier_description(call: ServiceCall) -> str:
    if ATTR_BARCODE in call.data:
        return f"barcode {call.data[ATTR_BARCODE]}"
    if ATTR_PRODUCT_ID in call.data:
        return f"product ID {call.data[ATTR_PRODUCT_ID]}"
    return f"product name {call.data[ATTR_PRODUCT_NAME]}"


async def _async_lookup(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Handle the read-only product lookup action."""
    resolver = entry.runtime_data.resolver

    try:
        if ATTR_BARCODE in call.data:
            result = await resolver.async_lookup_by_barcode(call.data[ATTR_BARCODE])
        elif ATTR_PRODUCT_ID in call.data:
            result = await resolver.async_lookup_by_product_id(
                call.data[ATTR_PRODUCT_ID]
            )
        else:
            result = await resolver.async_lookup_by_product_name(
                call.data[ATTR_PRODUCT_NAME]
            )
    except GrocyNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_not_found",
            translation_placeholders={
                "identifier": _identifier_description(call),
            },
        ) from err
    except GrocyAmbiguousProductError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_name_ambiguous",
            translation_placeholders={
                "product_name": call.data[ATTR_PRODUCT_NAME],
            },
        ) from err
    except GrocyInvalidAuthError as err:
        entry.async_start_reauth_if_available(call.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_auth_runtime",
        ) from err
    except GrocyInvalidResponseError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_response_runtime",
        ) from err
    except (GrocyCannotConnectError, GrocyApiError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect_runtime",
        ) from err

    return result.as_service_response()


@callback
def async_setup_services(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> None:
    """Register Grocy Stock Manager actions."""

    async def async_lookup(call: ServiceCall) -> ServiceResponse:
        return await _async_lookup(entry, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOOKUP,
        async_lookup,
        schema=LOOKUP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Grocy Stock Manager actions."""
    hass.services.async_remove(DOMAIN, SERVICE_LOOKUP)

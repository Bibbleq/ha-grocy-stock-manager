"""Home Assistant actions for Grocy Stock Manager."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
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
    GrocyMutationOutcomeUnknownError,
    GrocyNotFoundError,
)
from .catalogue import (
    CatalogueBarcodeConflictError,
    CatalogueLocationNotFoundError,
    CatalogueQuantityUnitNotFoundError,
)
from .const import (
    ATTR_AMOUNT,
    ATTR_BARCODE,
    ATTR_LOCATION_ID,
    ATTR_LOCATION_NAME,
    ATTR_PRODUCT_ID,
    ATTR_PRODUCT_NAME,
    ATTR_QUANTITY_UNIT_ID,
    ATTR_QUANTITY_UNIT_NAME,
    ATTR_REQUEST_ID,
    ATTR_SOURCE,
    DOMAIN,
    SERVICE_ADD,
    SERVICE_CONFIRM_PRODUCT,
    SERVICE_CONSUME,
    SERVICE_LOOKUP,
)
from .transactions import (
    TransactionInsufficientStockError,
    TransactionLocationAmbiguousError,
    TransactionLocationNotFoundError,
    TransactionRequestConflictError,
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


def _positive_decimal(value: Any) -> Decimal:
    """Return one finite positive Decimal without binary-float conversion."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as err:
        raise vol.Invalid("amount must be a number") from err
    if not amount.is_finite() or amount <= 0:
        raise vol.Invalid("amount must be a finite number greater than zero")
    return amount


def _at_most_one_location(data: dict[str, Any]) -> dict[str, Any]:
    if ATTR_LOCATION_ID in data and ATTR_LOCATION_NAME in data:
        raise vol.Invalid("only one of location_id or location_name is allowed")
    return data


MUTATION_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_BARCODE): vol.All(_non_empty_string, vol.Length(max=128)),
            vol.Optional(ATTR_PRODUCT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_PRODUCT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Required(ATTR_AMOUNT, default=Decimal("1")): _positive_decimal,
            vol.Optional(ATTR_LOCATION_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_LOCATION_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Required(ATTR_REQUEST_ID): vol.All(
                _non_empty_string, vol.Length(max=128)
            ),
            vol.Optional(ATTR_SOURCE, default="home_assistant"): vol.All(
                _non_empty_string, vol.Length(max=64)
            ),
        }
    ),
    _exactly_one_identifier,
    _at_most_one_location,
)


def _exactly_one_location(data: dict[str, Any]) -> dict[str, Any]:
    if sum(key in data for key in (ATTR_LOCATION_ID, ATTR_LOCATION_NAME)) != 1:
        raise vol.Invalid("exactly one of location_id or location_name is required")
    return data


def _at_most_one_quantity_unit(data: dict[str, Any]) -> dict[str, Any]:
    if ATTR_QUANTITY_UNIT_ID in data and ATTR_QUANTITY_UNIT_NAME in data:
        raise vol.Invalid(
            "only one of quantity_unit_id or quantity_unit_name is allowed"
        )
    return data


CONFIRM_PRODUCT_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_BARCODE): vol.All(
                _non_empty_string, vol.Length(max=128)
            ),
            vol.Required(ATTR_PRODUCT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Optional(ATTR_LOCATION_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_LOCATION_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Optional(ATTR_QUANTITY_UNIT_ID): vol.All(
                vol.Coerce(int), vol.Range(min=1)
            ),
            vol.Optional(ATTR_QUANTITY_UNIT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
        }
    ),
    _exactly_one_location,
    _at_most_one_quantity_unit,
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


async def _async_resolve_for_mutation(
    entry: GrocyStockManagerConfigEntry, call: ServiceCall
):
    resolver = entry.runtime_data.resolver
    if ATTR_BARCODE in call.data:
        return await resolver.async_lookup_by_barcode(call.data[ATTR_BARCODE])
    if ATTR_PRODUCT_ID in call.data:
        return await resolver.async_lookup_by_product_id(call.data[ATTR_PRODUCT_ID])
    return await resolver.async_lookup_by_product_name(call.data[ATTR_PRODUCT_NAME])


async def _async_mutate(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
    operation: str,
) -> ServiceResponse:
    """Resolve, validate, execute, and verify one stock mutation."""
    try:
        lookup = await _async_resolve_for_mutation(entry, call)
        return await entry.runtime_data.transactions.async_execute(
            operation,
            lookup,
            amount=call.data[ATTR_AMOUNT],
            request_id=call.data[ATTR_REQUEST_ID],
            location_id=call.data.get(ATTR_LOCATION_ID),
            location_name=call.data.get(ATTR_LOCATION_NAME),
            source=call.data[ATTR_SOURCE],
        )
    except GrocyNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_not_found",
            translation_placeholders={"identifier": _identifier_description(call)},
        ) from err
    except GrocyAmbiguousProductError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_name_ambiguous",
            translation_placeholders={"product_name": call.data[ATTR_PRODUCT_NAME]},
        ) from err
    except TransactionLocationNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="location_not_found",
        ) from err
    except TransactionLocationAmbiguousError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="location_ambiguous",
        ) from err
    except TransactionInsufficientStockError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="insufficient_stock",
        ) from err
    except TransactionRequestConflictError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="request_id_conflict",
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


async def _async_confirm_product(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Create or map only the exact product the user has confirmed."""
    try:
        return await entry.runtime_data.catalogue.async_confirm_product(
            barcode=call.data[ATTR_BARCODE],
            product_name=call.data[ATTR_PRODUCT_NAME],
            location_id=call.data.get(ATTR_LOCATION_ID),
            location_name=call.data.get(ATTR_LOCATION_NAME),
            quantity_unit_id=call.data.get(ATTR_QUANTITY_UNIT_ID),
            quantity_unit_name=call.data.get(ATTR_QUANTITY_UNIT_NAME),
        )
    except CatalogueBarcodeConflictError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="barcode_conflict",
        ) from err
    except CatalogueLocationNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="catalogue_location_not_found",
            translation_placeholders={
                "requested": err.requested,
                "available": ", ".join(err.available_names) or "none",
            },
        ) from err
    except CatalogueQuantityUnitNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="quantity_unit_not_found",
            translation_placeholders={
                "requested": err.requested,
                "available": ", ".join(err.available_names) or "none",
            },
        ) from err
    except GrocyAmbiguousProductError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_name_ambiguous",
            translation_placeholders={"product_name": call.data[ATTR_PRODUCT_NAME]},
        ) from err
    except GrocyInvalidAuthError as err:
        entry.async_start_reauth_if_available(call.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_auth_runtime",
        ) from err
    except GrocyMutationOutcomeUnknownError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="catalogue_outcome_unknown",
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


@callback
def async_setup_services(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> None:
    """Register Grocy Stock Manager actions."""

    async def async_lookup(call: ServiceCall) -> ServiceResponse:
        return await _async_lookup(entry, call)

    async def async_add(call: ServiceCall) -> ServiceResponse:
        return await _async_mutate(entry, call, "add")

    async def async_consume(call: ServiceCall) -> ServiceResponse:
        return await _async_mutate(entry, call, "consume")

    async def async_confirm_product(call: ServiceCall) -> ServiceResponse:
        return await _async_confirm_product(entry, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOOKUP,
        async_lookup,
        schema=LOOKUP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD,
        async_add,
        schema=MUTATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONSUME,
        async_consume,
        schema=MUTATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIRM_PRODUCT,
        async_confirm_product,
        schema=CONFIRM_PRODUCT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Grocy Stock Manager actions."""
    hass.services.async_remove(DOMAIN, SERVICE_LOOKUP)
    hass.services.async_remove(DOMAIN, SERVICE_ADD)
    hass.services.async_remove(DOMAIN, SERVICE_CONSUME)
    hass.services.async_remove(DOMAIN, SERVICE_CONFIRM_PRODUCT)

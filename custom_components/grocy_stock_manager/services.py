"""Home Assistant actions for Grocy Stock Manager."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import SimpleNamespace
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
    ATTR_AGENT_ID,
    ATTR_AI_TASK_ENTITY_ID,
    ATTR_AMOUNT,
    ATTR_BARCODE,
    ATTR_BARCODE_AMOUNT,
    ATTR_CANDIDATE_LIMIT,
    ATTR_CANONICAL_NAME,
    ATTR_CONFIRMATION_TOKEN,
    ATTR_DRY_RUN,
    ATTR_JOB_ID,
    ATTR_LEARN_ALIAS,
    ATTR_LOCATION_ID,
    ATTR_LOCATION_NAME,
    ATTR_NOTE,
    ATTR_OPERATION,
    ATTR_ORIGINAL_REQUEST_ID,
    ATTR_PRODUCT_ALIASES,
    ATTR_PRODUCT_ID,
    ATTR_PRODUCT_ID_TO_KEEP,
    ATTR_PRODUCT_ID_TO_REMOVE,
    ATTR_PRODUCT_NAME,
    ATTR_PRODUCT_PHRASE,
    ATTR_QUANTITY_UNIT_ID,
    ATTR_QUANTITY_UNIT_NAME,
    ATTR_REQUEST_ID,
    ATTR_SOURCE,
    DOMAIN,
    SERVICE_ACKNOWLEDGE_RECONCILIATION,
    SERVICE_ADD,
    SERVICE_COMPLETE_PRODUCT_IDENTIFICATION,
    SERVICE_CONFIRM_PRODUCT,
    SERVICE_CONFIRM_PRODUCT_IDENTIFICATION,
    SERVICE_CONFIRM_PRODUCT_TRANSACTION,
    SERVICE_CONFIRM_VOICE_TRANSACTION,
    SERVICE_CONSUME,
    SERVICE_LEARN_PRODUCT_ALIAS,
    SERVICE_LIST_PRODUCT_ALIASES,
    SERVICE_LOOKUP,
    SERVICE_MERGE_PRODUCTS,
    SERVICE_OVERRIDE_PRODUCT_IDENTIFICATION,
    SERVICE_REJECT_PRODUCT_IDENTIFICATION,
    SERVICE_REMOVE_PRODUCT_ALIAS,
    SERVICE_RESEARCH_BARCODE,
    SERVICE_RESOLVE_PRODUCT_PHRASE,
    SERVICE_START_PRODUCT_IDENTIFICATION,
    SERVICE_UNDO_TRANSACTION,
    SERVICE_VOICE_TRANSACTION,
)
from .identification import IdentificationRequestConflictError
from .journal import is_undoable_result
from .merges import (
    MergeAliasConflictError,
    MergeNameConflictError,
    MergeQuantityUnitMismatchError,
    MergeSameProductError,
)
from .transactions import (
    TransactionInsufficientStockError,
    TransactionLocationAmbiguousError,
    TransactionLocationNotFoundError,
    TransactionRequestConflictError,
)
from .voice import (
    VoiceAliasConflictError,
    VoiceAliasFieldMissingError,
    VoiceCandidateNotAllowedError,
    VoiceConfirmationNotFoundError,
    normalise_product_phrase,
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

RESEARCH_BARCODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BARCODE): vol.All(
            _non_empty_string, vol.Length(max=128)
        ),
        vol.Required(ATTR_AI_TASK_ENTITY_ID): vol.All(
            _non_empty_string, vol.Length(max=255)
        ),
    }
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


def _product_aliases(value: Any) -> tuple[str, ...]:
    """Accept a UI list or comma/newline-delimited aliases and deduplicate it."""
    if value in (None, ""):
        return ()
    raw = value.replace("\n", ",").split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple)):
        raise vol.Invalid("product_aliases must be a list or delimited string")
    aliases: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise vol.Invalid("every product alias must be text")
        alias = item.strip()
        if not alias:
            continue
        if len(alias) > 255:
            raise vol.Invalid("product aliases must be at most 255 characters")
        normalised = normalise_product_phrase(alias)
        if normalised and normalised not in seen:
            aliases.append(alias)
            seen.add(normalised)
    return tuple(aliases)


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


def _identification_source(data: dict[str, Any]) -> dict[str, Any]:
    """Require either a trusted candidate or an agent for unknown-product work."""
    candidate = data.get(ATTR_PRODUCT_NAME)
    agent_id = data.get(ATTR_AGENT_ID)
    if not candidate and not agent_id:
        raise vol.Invalid("agent_id is required when product_name is not supplied")
    if data.get(ATTR_PRODUCT_ALIASES) and not candidate:
        raise vol.Invalid("product_aliases requires product_name")
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
            vol.Optional(ATTR_BARCODE_AMOUNT): _positive_decimal,
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


CONFIRM_PRODUCT_TRANSACTION_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_BARCODE): vol.All(
                _non_empty_string, vol.Length(max=128)
            ),
            vol.Required(ATTR_PRODUCT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Required(ATTR_OPERATION): vol.In(("add", "consume")),
            vol.Required(ATTR_AMOUNT, default=Decimal("1")): _positive_decimal,
            vol.Optional(ATTR_BARCODE_AMOUNT): _positive_decimal,
            vol.Optional(ATTR_LOCATION_ID): vol.All(
                vol.Coerce(int), vol.Range(min=1)
            ),
            vol.Optional(ATTR_LOCATION_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Optional(ATTR_QUANTITY_UNIT_ID): vol.All(
                vol.Coerce(int), vol.Range(min=1)
            ),
            vol.Optional(ATTR_QUANTITY_UNIT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Required(ATTR_REQUEST_ID): vol.All(
                _non_empty_string, vol.Length(max=128)
            ),
            vol.Optional(ATTR_SOURCE, default="scanner"): vol.All(
                _non_empty_string, vol.Length(max=64)
            ),
        }
    ),
    _at_most_one_location,
    _at_most_one_quantity_unit,
)


RESOLVE_PRODUCT_PHRASE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PRODUCT_PHRASE): vol.All(
            _non_empty_string, vol.Length(max=255)
        ),
        vol.Optional(ATTR_OPERATION, default="consume"): vol.In(
            ("add", "consume")
        ),
        vol.Optional(ATTR_CANDIDATE_LIMIT, default=3): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=5)
        ),
    }
)


VOICE_TRANSACTION_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_OPERATION): vol.In(("add", "consume")),
            vol.Required(ATTR_PRODUCT_PHRASE): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Required(ATTR_AMOUNT, default=Decimal("1")): _positive_decimal,
            vol.Optional(ATTR_LOCATION_ID): vol.All(
                vol.Coerce(int), vol.Range(min=1)
            ),
            vol.Optional(ATTR_LOCATION_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Required(ATTR_REQUEST_ID): vol.All(
                _non_empty_string, vol.Length(max=128)
            ),
            vol.Optional(ATTR_SOURCE, default="voice"): vol.All(
                _non_empty_string, vol.Length(max=64)
            ),
            vol.Optional(ATTR_CANDIDATE_LIMIT, default=3): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=5)
            ),
        }
    ),
    _at_most_one_location,
)


CONFIRM_VOICE_TRANSACTION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRMATION_TOKEN): vol.All(
            _non_empty_string, vol.Length(max=128)
        ),
        vol.Required(ATTR_PRODUCT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_LEARN_ALIAS, default=True): cv.boolean,
    }
)


PRODUCT_ALIAS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PRODUCT_PHRASE): vol.All(
            _non_empty_string, vol.Length(max=255)
        ),
        vol.Required(ATTR_PRODUCT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)


REMOVE_PRODUCT_ALIAS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PRODUCT_PHRASE): vol.All(
            _non_empty_string, vol.Length(max=255)
        )
    }
)


MERGE_PRODUCTS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PRODUCT_ID_TO_KEEP): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
        vol.Required(ATTR_PRODUCT_ID_TO_REMOVE): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
        vol.Optional(ATTR_CANONICAL_NAME): vol.All(
            _non_empty_string, vol.Length(max=255)
        ),
        vol.Required(ATTR_REQUEST_ID): vol.All(
            _non_empty_string, vol.Length(max=128)
        ),
        vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
    }
)


UNDO_TRANSACTION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ORIGINAL_REQUEST_ID): vol.All(
            _non_empty_string, vol.Length(max=128)
        )
    }
)


ACKNOWLEDGE_RECONCILIATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ORIGINAL_REQUEST_ID): vol.All(
            _non_empty_string, vol.Length(max=128)
        ),
        vol.Required(ATTR_NOTE): vol.All(_non_empty_string, vol.Length(max=255)),
    }
)


START_PRODUCT_IDENTIFICATION_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_BARCODE): vol.All(
                _non_empty_string, vol.Length(max=128)
            ),
            vol.Required(ATTR_OPERATION): vol.In(("add", "consume")),
            vol.Required(ATTR_AMOUNT, default=Decimal("1")): _positive_decimal,
            vol.Optional(ATTR_LOCATION_ID): vol.All(
                vol.Coerce(int), vol.Range(min=1)
            ),
            vol.Optional(ATTR_LOCATION_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Optional(ATTR_QUANTITY_UNIT_ID): vol.All(
                vol.Coerce(int), vol.Range(min=1)
            ),
            vol.Optional(ATTR_QUANTITY_UNIT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Required(ATTR_REQUEST_ID): vol.All(
                _non_empty_string, vol.Length(max=128)
            ),
            vol.Optional(ATTR_SOURCE, default="scanner"): vol.All(
                _non_empty_string, vol.Length(max=64)
            ),
            vol.Optional(ATTR_AGENT_ID): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Optional(ATTR_PRODUCT_NAME): vol.All(
                _non_empty_string, vol.Length(max=255)
            ),
            vol.Optional(ATTR_PRODUCT_ALIASES, default=()): _product_aliases,
        }
    ),
    _at_most_one_location,
    _at_most_one_quantity_unit,
    _identification_source,
)


IDENTIFICATION_JOB_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): vol.All(
            _non_empty_string, vol.Length(max=64)
        )
    }
)


OVERRIDE_PRODUCT_IDENTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): vol.All(
            _non_empty_string, vol.Length(max=128)
        ),
        vol.Optional(ATTR_PRODUCT_NAME): vol.All(
            _non_empty_string, vol.Length(max=255)
        ),
        vol.Optional(ATTR_PRODUCT_ALIASES, default=()): _product_aliases,
    }
)


CONFIRM_PRODUCT_IDENTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): vol.All(
            _non_empty_string, vol.Length(max=128)
        ),
        vol.Required(ATTR_PRODUCT_NAME): vol.All(
            _non_empty_string, vol.Length(max=255)
        ),
        vol.Optional(ATTR_PRODUCT_ALIASES, default=()): _product_aliases,
        vol.Optional(
            ATTR_BARCODE_AMOUNT, default=Decimal("1")
        ): _positive_decimal,
    }
)


COMPLETE_PRODUCT_IDENTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): vol.All(
            _non_empty_string, vol.Length(max=64)
        ),
        vol.Required(ATTR_PRODUCT_NAME): vol.All(
            _non_empty_string, vol.Length(max=255)
        ),
    }
)


def _identifier_description(call: ServiceCall) -> str:
    if ATTR_BARCODE in call.data:
        return f"barcode {call.data[ATTR_BARCODE]}"
    if ATTR_PRODUCT_ID in call.data:
        return f"product ID {call.data[ATTR_PRODUCT_ID]}"
    return f"product name {call.data[ATTR_PRODUCT_NAME]}"


@callback
def _request_inventory_refresh(
    entry: GrocyStockManagerConfigEntry,
    hass: HomeAssistant,
) -> None:
    """Refresh the read-only inventory shortly after a successful write."""
    hass.async_create_task(entry.runtime_data.coordinator.async_request_refresh())


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


def _mutation_rejected_response(
    call: ServiceCall,
    operation: str,
    *,
    error_code: str,
    message: str,
    lookup: Any | None = None,
) -> ServiceResponse:
    """Return an expected, fail-closed rejection without an opaque HTTP 500."""
    response: dict[str, Any] = {
        "response_version": 1,
        "success": False,
        "outcome": "rejected",
        "stock_changed": False,
        "operation": operation,
        "request_id": call.data[ATTR_REQUEST_ID],
        "source": call.data[ATTR_SOURCE],
        "error_code": error_code,
        "message": message,
        "requires_reconciliation": False,
    }
    if lookup is not None:
        response["product_id"] = lookup.product.id
        response["product_name"] = lookup.product.name
        response["stock_total"] = float(lookup.product.stock_total)
        response["stock_locations"] = [
            item.as_dict() for item in lookup.stock_locations
        ]
    return response


async def _async_mutate(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
    operation: str,
) -> ServiceResponse:
    """Resolve, validate, execute, and verify one stock mutation."""
    lookup = None
    try:
        lookup = await _async_resolve_for_mutation(entry, call)
        response = await entry.runtime_data.transactions.async_execute(
            operation,
            lookup,
            amount=call.data[ATTR_AMOUNT],
            request_id=call.data[ATTR_REQUEST_ID],
            location_id=call.data.get(ATTR_LOCATION_ID),
            location_name=call.data.get(ATTR_LOCATION_NAME),
            source=call.data[ATTR_SOURCE],
        )
        _request_inventory_refresh(entry, call.hass)
        return response
    except GrocyNotFoundError:
        return _mutation_rejected_response(
            call,
            operation,
            error_code="product_not_found",
            message=(
                f"No Grocy product matched {_identifier_description(call)}. "
                "No stock was changed."
            ),
        )
    except GrocyAmbiguousProductError:
        return _mutation_rejected_response(
            call,
            operation,
            error_code="product_name_ambiguous",
            message=(
                "More than one Grocy product matched that name. Use a barcode or "
                "product ID; no stock was changed."
            ),
        )
    except TransactionLocationNotFoundError:
        return _mutation_rejected_response(
            call,
            operation,
            error_code="location_not_found",
            message=(
                "No usable Grocy stock location could be resolved. "
                "No stock was changed."
            ),
            lookup=lookup,
        )
    except TransactionLocationAmbiguousError:
        return _mutation_rejected_response(
            call,
            operation,
            error_code="location_ambiguous",
            message=(
                "This product is stocked in more than one possible location. "
                "Choose a location; no stock was changed."
            ),
            lookup=lookup,
        )
    except TransactionInsufficientStockError:
        return _mutation_rejected_response(
            call,
            operation,
            error_code="insufficient_stock",
            message=(
                "The selected location does not contain enough stock. "
                "No stock was changed."
            ),
            lookup=lookup,
        )
    except TransactionRequestConflictError:
        return _mutation_rejected_response(
            call,
            operation,
            error_code="request_id_conflict",
            message=(
                "That request ID was already used for a different transaction. "
                "No new stock change was made."
            ),
            lookup=lookup,
        )
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
            barcode_amount=call.data.get(ATTR_BARCODE_AMOUNT),
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


def _transaction_rejection(
    error: Exception,
    *,
    operation: str,
    request_id: str,
    source: str,
    catalogue: dict[str, Any] | None = None,
) -> ServiceResponse:
    """Return a stable fail-closed response for an expected stock rejection."""
    if isinstance(error, TransactionLocationNotFoundError):
        code = "location_not_found"
        message = "No usable stock location could be resolved."
    elif isinstance(error, TransactionLocationAmbiguousError):
        code = "location_ambiguous"
        message = "The product is stocked in more than one possible location."
    elif isinstance(error, TransactionInsufficientStockError):
        code = "insufficient_stock"
        message = "The selected location does not contain enough stock."
    else:
        code = "request_id_conflict"
        message = "That request ID was already used for different work."
    return {
        "response_version": 1,
        "success": False,
        "status": "rejected",
        "outcome": "rejected",
        "stock_changed": False,
        "operation": operation,
        "request_id": request_id,
        "source": source,
        "error_code": code,
        "message": f"{message} No stock was changed.",
        "requires_reconciliation": False,
        "catalogue": catalogue,
    }


async def _async_confirm_product_transaction(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Confirm catalogue identity and execute the originally captured intent."""
    catalogue: dict[str, Any] | None = None
    try:
        catalogue = await entry.runtime_data.catalogue.async_confirm_product(
            barcode=call.data[ATTR_BARCODE],
            product_name=call.data[ATTR_PRODUCT_NAME],
            location_id=call.data.get(ATTR_LOCATION_ID),
            location_name=call.data.get(ATTR_LOCATION_NAME),
            quantity_unit_id=call.data.get(ATTR_QUANTITY_UNIT_ID),
            quantity_unit_name=call.data.get(ATTR_QUANTITY_UNIT_NAME),
            barcode_amount=call.data.get(ATTR_BARCODE_AMOUNT),
        )
        lookup = await entry.runtime_data.resolver.async_lookup_by_barcode(
            call.data[ATTR_BARCODE]
        )
        transaction = await entry.runtime_data.transactions.async_execute(
            call.data[ATTR_OPERATION],
            lookup,
            amount=(
                call.data[ATTR_AMOUNT]
                * call.data.get(ATTR_BARCODE_AMOUNT, Decimal("1"))
            ),
            request_id=call.data[ATTR_REQUEST_ID],
            location_id=call.data.get(ATTR_LOCATION_ID),
            location_name=call.data.get(ATTR_LOCATION_NAME),
            source=call.data[ATTR_SOURCE],
        )
        _request_inventory_refresh(entry, call.hass)
        return {
            "response_version": 1,
            "success": transaction.get("outcome") == "committed",
            "status": transaction.get("outcome"),
            "stock_changed": transaction.get("outcome") == "committed",
            "catalogue": catalogue,
            "transaction": transaction,
        }
    except (
        TransactionLocationNotFoundError,
        TransactionLocationAmbiguousError,
        TransactionInsufficientStockError,
        TransactionRequestConflictError,
    ) as err:
        _request_inventory_refresh(entry, call.hass)
        return _transaction_rejection(
            err,
            operation=call.data[ATTR_OPERATION],
            request_id=call.data[ATTR_REQUEST_ID],
            source=call.data[ATTR_SOURCE],
            catalogue=catalogue,
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


async def _async_undo_transaction(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Compensate one exact committed stock mutation once."""
    original_id = call.data[ATTR_ORIGINAL_REQUEST_ID]
    original = await entry.runtime_data.journal.async_get(original_id)
    if original is None:
        return {
            "response_version": 1,
            "success": False,
            "status": "rejected",
            "error_code": "transaction_not_found",
            "message": "That transaction is no longer in the activity journal.",
            "stock_changed": False,
        }
    result = original["result"]
    if result.get("undone_by"):
        return {
            "response_version": 1,
            "success": False,
            "status": "rejected",
            "error_code": "already_undone",
            "message": "That transaction has already been undone.",
            "stock_changed": False,
            "undone_by": result["undone_by"],
        }
    if not is_undoable_result(result):
        return {
            "response_version": 1,
            "success": False,
            "status": "rejected",
            "error_code": "transaction_not_undoable",
            "message": "Only a verified add or consume can be undone.",
            "stock_changed": False,
        }

    undo_operation = "consume" if result["operation"] == "add" else "add"
    undo_id = f"undo:{sha256(original_id.encode()).hexdigest()[:32]}"
    try:
        lookup = await entry.runtime_data.resolver.async_lookup_by_product_id(
            int(result["product_id"])
        )
        transaction = await entry.runtime_data.transactions.async_execute(
            undo_operation,
            lookup,
            amount=Decimal(str(result["amount"])),
            request_id=undo_id,
            location_id=int(result["location_id"]),
            location_name=None,
            source=f"undo:{str(result.get('source', 'unknown'))}"[:64],
        )
    except (
        TransactionLocationNotFoundError,
        TransactionLocationAmbiguousError,
        TransactionInsufficientStockError,
        TransactionRequestConflictError,
    ) as err:
        return _transaction_rejection(
            err,
            operation=undo_operation,
            request_id=undo_id,
            source="undo",
        )
    except GrocyNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_not_found",
            translation_placeholders={
                "identifier": f"product ID {result['product_id']}"
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

    if transaction.get("outcome") == "committed":
        await entry.runtime_data.journal.async_update_result(
            undo_id, {"undo_of": original_id}
        )
        await entry.runtime_data.journal.async_update_result(
            original_id, {"undone_by": undo_id}
        )
        transaction = {
            **transaction,
            "undo_of": original_id,
        }
    _request_inventory_refresh(entry, call.hass)
    return {
        "response_version": 1,
        "success": transaction.get("outcome") == "committed",
        "status": transaction.get("outcome"),
        "stock_changed": transaction.get("outcome") == "committed",
        "original_request_id": original_id,
        "transaction": transaction,
    }


async def _async_acknowledge_reconciliation(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Clear a health latch only after an explicit human reconciliation."""
    result = await entry.runtime_data.journal.async_acknowledge_reconciliation(
        call.data[ATTR_ORIGINAL_REQUEST_ID], call.data[ATTR_NOTE]
    )
    if result is None:
        return {
            "response_version": 1,
            "success": False,
            "status": "rejected",
            "message": "No unresolved transaction matched that request ID.",
        }
    _request_inventory_refresh(entry, call.hass)
    return {
        "response_version": 1,
        "success": True,
        "status": "reconciled",
        "request_id": call.data[ATTR_ORIGINAL_REQUEST_ID],
        "result": result,
    }


async def _async_resolve_product_phrase(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Return safe live candidates for one spoken product phrase."""
    try:
        resolution = await entry.runtime_data.voice_resolver.async_resolve(
            call.data[ATTR_PRODUCT_PHRASE],
            operation=call.data[ATTR_OPERATION],
            candidate_limit=call.data[ATTR_CANDIDATE_LIMIT],
        )
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
    return resolution.as_dict()


async def _async_voice_transaction(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Resolve a spoken product and mutate only an authoritative match."""
    try:
        response = await entry.runtime_data.voice.async_process(
            operation=call.data[ATTR_OPERATION],
            product_phrase=call.data[ATTR_PRODUCT_PHRASE],
            amount=call.data[ATTR_AMOUNT],
            request_id=call.data[ATTR_REQUEST_ID],
            location_id=call.data.get(ATTR_LOCATION_ID),
            location_name=call.data.get(ATTR_LOCATION_NAME),
            source=call.data[ATTR_SOURCE],
            candidate_limit=call.data[ATTR_CANDIDATE_LIMIT],
        )
        _request_inventory_refresh(entry, call.hass)
        return response
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


async def _async_confirm_voice_transaction(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Confirm one offered candidate and execute the staged transaction."""
    try:
        response = await entry.runtime_data.voice.async_confirm(
            confirmation_token=call.data[ATTR_CONFIRMATION_TOKEN],
            product_id=call.data[ATTR_PRODUCT_ID],
            learn_alias=call.data[ATTR_LEARN_ALIAS],
        )
        _request_inventory_refresh(entry, call.hass)
        return response
    except VoiceConfirmationNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_confirmation_not_found",
        ) from err
    except VoiceCandidateNotAllowedError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_candidate_not_allowed",
        ) from err
    except VoiceAliasConflictError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_alias_conflict",
        ) from err
    except VoiceAliasFieldMissingError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_alias_field_missing",
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
    except GrocyNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_not_found",
            translation_placeholders={
                "identifier": f"product ID {call.data[ATTR_PRODUCT_ID]}"
            },
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
            translation_key="voice_alias_outcome_unknown",
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


async def _async_learn_product_alias(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Learn an explicitly confirmed phrase for one exact product ID."""
    try:
        lookup = await entry.runtime_data.resolver.async_lookup_by_product_id(
            call.data[ATTR_PRODUCT_ID]
        )
        learned = await entry.runtime_data.voice_aliases.async_learn(
            call.data[ATTR_PRODUCT_PHRASE],
            lookup.product.id,
        )
    except VoiceAliasConflictError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_alias_conflict",
        ) from err
    except VoiceAliasFieldMissingError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_alias_field_missing",
        ) from err
    except GrocyNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="product_not_found",
            translation_placeholders={
                "identifier": f"product ID {call.data[ATTR_PRODUCT_ID]}"
            },
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
            translation_key="voice_alias_outcome_unknown",
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
    return {
        "response_version": 1,
        "success": True,
        "product_phrase": call.data[ATTR_PRODUCT_PHRASE],
        "normalised_phrase": normalise_product_phrase(
            call.data[ATTR_PRODUCT_PHRASE]
        ),
        "product_id": lookup.product.id,
        "product_name": lookup.product.name,
        "alias_learned": learned,
    }


async def _async_remove_product_alias(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Forget one product phrase without changing stock or Grocy data."""
    try:
        removed = await entry.runtime_data.voice_aliases.async_remove(
            call.data[ATTR_PRODUCT_PHRASE]
        )
    except VoiceAliasConflictError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_alias_conflict",
        ) from err
    except VoiceAliasFieldMissingError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_alias_field_missing",
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
            translation_key="voice_alias_outcome_unknown",
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
    return {
        "response_version": 1,
        "success": True,
        "product_phrase": call.data[ATTR_PRODUCT_PHRASE],
        "alias_removed": removed,
    }


async def _async_list_product_aliases(
    entry: GrocyStockManagerConfigEntry,
) -> ServiceResponse:
    """Return all learned aliases, enriched with current canonical names."""
    try:
        aliases = await entry.runtime_data.voice_aliases.async_list()
    except GrocyInvalidAuthError as err:
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
    return {
        "response_version": 1,
        "success": True,
        "aliases": aliases,
    }


async def _async_start_product_identification(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Persist an unknown scan and start slow AI work in the background."""
    try:
        return await entry.runtime_data.identification.async_start(
            barcode=call.data[ATTR_BARCODE],
            operation=call.data[ATTR_OPERATION],
            amount=call.data[ATTR_AMOUNT],
            request_id=call.data[ATTR_REQUEST_ID],
            location_id=call.data.get(ATTR_LOCATION_ID),
            location_name=call.data.get(ATTR_LOCATION_NAME),
            quantity_unit_id=call.data.get(ATTR_QUANTITY_UNIT_ID),
            quantity_unit_name=(
                call.data.get(ATTR_QUANTITY_UNIT_NAME)
                or (
                    "Pack"
                    if call.data.get(ATTR_QUANTITY_UNIT_ID) is None
                    else None
                )
            ),
            source=call.data[ATTR_SOURCE],
            agent_id=call.data.get(ATTR_AGENT_ID, "catalogue"),
            candidate_name=call.data.get(ATTR_PRODUCT_NAME),
            product_aliases=call.data[ATTR_PRODUCT_ALIASES],
        )
    except RuntimeError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="identification_queue_full",
        ) from err
    except IdentificationRequestConflictError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="identification_request_conflict",
        ) from err


async def _async_confirm_product_identification(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Confirm one queued identity and its immutable captured transaction."""
    manager = entry.runtime_data.identification
    job_id = call.data[ATTR_JOB_ID]
    product_name = call.data[ATTR_PRODUCT_NAME]
    aliases = call.data[ATTR_PRODUCT_ALIASES]
    barcode_amount = call.data.get(ATTR_BARCODE_AMOUNT, Decimal("1"))
    current = manager.get(job_id)
    if current is None:
        return {
            "response_version": 1,
            "success": False,
            "status": "not_found",
            "stock_changed": False,
            "queue": manager.queue_summary(),
        }

    recovered = await manager.async_recover_job(job_id)
    if recovered is not None:
        return recovered
    if current.status == "rejected":
        return {
            "response_version": 1,
            "success": False,
            "status": "rejected",
            "stock_changed": False,
            "job": current.as_public_dict(),
            "queue": manager.queue_summary(job_id),
        }
    if current.status == "completed":
        return {
            "response_version": 1,
            "success": True,
            "status": "committed",
            "stock_changed": True,
            "replayed": True,
            "job": current.as_public_dict(),
            "queue": manager.queue_summary(job_id),
            "warnings": ["The completed transaction is no longer in the journal."],
        }

    begun = await manager.async_begin_confirmation(
        job_id,
        product_name,
        aliases,
        barcode_amount,
    )
    if begun is None:
        return {
            "response_version": 1,
            "success": False,
            "status": "not_found",
            "stock_changed": False,
            "queue": manager.queue_summary(),
        }
    if (
        begun.confirmed_product_name is not None
        and begun.confirmed_product_name.casefold() != product_name.casefold()
    ):
        return {
            "response_version": 1,
            "success": False,
            "status": "rejected",
            "stock_changed": False,
            "error_code": "confirmation_already_in_progress",
            "message": "This queue item is already confirming a different name.",
            "job": begun.as_public_dict(),
            "queue": manager.queue_summary(job_id),
        }

    confirmation_call = SimpleNamespace(
        hass=call.hass,
        data={
            ATTR_BARCODE: begun.barcode,
            ATTR_PRODUCT_NAME: begun.confirmed_product_name or product_name,
            ATTR_OPERATION: begun.operation,
            ATTR_AMOUNT: begun.amount,
            ATTR_BARCODE_AMOUNT: (
                getattr(begun, "confirmed_barcode_amount", None)
                or Decimal("1")
            ),
            ATTR_REQUEST_ID: begun.confirmation_request_id,
            ATTR_SOURCE: begun.source,
            **(
                {ATTR_LOCATION_ID: begun.location_id}
                if begun.location_id is not None
                else {}
            ),
            **(
                {ATTR_LOCATION_NAME: begun.location_name}
                if begun.location_name is not None
                else {}
            ),
            **(
                {ATTR_QUANTITY_UNIT_ID: begun.quantity_unit_id}
                if begun.quantity_unit_id is not None
                else {}
            ),
            **(
                {ATTR_QUANTITY_UNIT_NAME: begun.quantity_unit_name}
                if begun.quantity_unit_name is not None
                else {}
            ),
        },
    )
    try:
        confirmation = await _async_confirm_product_transaction(
            entry, confirmation_call
        )
    except Exception:
        await manager.async_return_for_review(
            job_id,
            error_code="confirmation_error_before_verified_commit",
            message="Confirmation failed safely; retry this queue item",
        )
        raise

    transaction = confirmation.get("transaction")
    if (
        confirmation.get("status") == "committed"
        and isinstance(transaction, dict)
        and transaction.get("outcome") == "committed"
    ):
        return await manager.async_mark_committed(
            job_id,
            str(transaction.get("product_name") or product_name),
            transaction=transaction,
            catalogue=confirmation.get("catalogue"),
            replayed=bool(transaction.get("replayed")),
        )
    if confirmation.get("status") == "rejected":
        returned = await manager.async_return_for_review(
            job_id,
            error_code=str(confirmation.get("error_code") or "transaction_rejected"),
            message=str(
                confirmation.get("message")
                or "The captured transaction was rejected safely; review it"
            ),
        )
        return {
            **confirmation,
            "job": returned.as_public_dict() if returned is not None else None,
            "queue": manager.queue_summary(job_id),
        }

    failed = await manager.async_mark_failed(
        job_id,
        error_code="transaction_outcome_unknown",
        message="The stock outcome could not be verified; reconcile before retrying",
    )
    return {
        **confirmation,
        "success": False,
        "status": "failed",
        "stock_changed": False,
        "requires_reconciliation": True,
        "job": failed.as_public_dict() if failed is not None else None,
        "queue": manager.queue_summary(job_id),
    }


def _identification_response(
    job: Any | None,
    *,
    action: str,
) -> ServiceResponse:
    if job is None:
        return {
            "response_version": 1,
            "success": False,
            "status": "not_found",
            "action": action,
        }
    return {
        "response_version": 1,
        "success": True,
        "status": job.status,
        "action": action,
        "job": job.as_public_dict(),
    }


async def _async_override_product_identification(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Let the tablet replace a slow AI lookup with immediate manual entry."""
    job = await entry.runtime_data.identification.async_override(
        call.data[ATTR_JOB_ID],
        product_name=call.data.get(ATTR_PRODUCT_NAME),
        product_aliases=call.data[ATTR_PRODUCT_ALIASES],
    )
    return _identification_response(job, action="override")


async def _async_complete_product_identification(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Close durable work after the captured stock transaction committed."""
    job = await entry.runtime_data.identification.async_complete(
        call.data[ATTR_JOB_ID], call.data[ATTR_PRODUCT_NAME]
    )
    return _identification_response(job, action="complete")


async def _async_reject_product_identification(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Reject one queued identity explicitly without changing stock."""
    job = await entry.runtime_data.identification.async_reject(
        call.data[ATTR_JOB_ID]
    )
    return _identification_response(job, action="reject")


async def _async_merge_products(
    entry: GrocyStockManagerConfigEntry,
    call: ServiceCall,
) -> ServiceResponse:
    """Plan or execute a guarded native Grocy product merge."""
    try:
        response = await entry.runtime_data.merges.async_execute(
            product_id_to_keep=call.data[ATTR_PRODUCT_ID_TO_KEEP],
            product_id_to_remove=call.data[ATTR_PRODUCT_ID_TO_REMOVE],
            canonical_name=call.data.get(ATTR_CANONICAL_NAME),
            request_id=call.data[ATTR_REQUEST_ID],
            dry_run=call.data[ATTR_DRY_RUN],
        )
        if not call.data[ATTR_DRY_RUN]:
            _request_inventory_refresh(entry, call.hass)
        return response
    except MergeSameProductError:
        error_code = "same_product"
        message = "The keep and remove product IDs must be different."
    except MergeQuantityUnitMismatchError:
        error_code = "quantity_unit_mismatch"
        message = "The products use different stock quantity units."
    except MergeAliasConflictError as err:
        error_code = "voice_alias_conflict"
        message = f"Voice alias {err.args[0]!r} belongs to another product."
    except MergeNameConflictError as err:
        error_code = "canonical_name_conflict"
        message = f"Canonical name {err.args[0]!r} belongs to another product."
    except TransactionRequestConflictError:
        error_code = "request_id_conflict"
        message = "That request ID was already used for different work."
    except GrocyNotFoundError:
        error_code = "product_not_found"
        message = "One of the requested Grocy products does not exist or is inactive."
    except GrocyInvalidAuthError as err:
        entry.async_start_reauth_if_available(call.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_auth_runtime",
        ) from err
    except GrocyMutationOutcomeUnknownError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="merge_outcome_unknown",
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

    return {
        "response_version": 1,
        "success": False,
        "outcome": "rejected",
        "stock_changed": False,
        "operation": "merge_products",
        "request_id": call.data[ATTR_REQUEST_ID],
        "dry_run": call.data[ATTR_DRY_RUN],
        "error_code": error_code,
        "message": message,
        "requires_reconciliation": False,
    }


@callback
def async_setup_services(
    hass: HomeAssistant,
    entry: GrocyStockManagerConfigEntry,
) -> None:
    """Register Grocy Stock Manager actions."""

    async def async_lookup(call: ServiceCall) -> ServiceResponse:
        return await _async_lookup(entry, call)

    async def async_research_barcode(call: ServiceCall) -> ServiceResponse:
        return await entry.runtime_data.researcher.async_research(
            call.data[ATTR_BARCODE],
            call.data[ATTR_AI_TASK_ENTITY_ID],
        )

    async def async_add(call: ServiceCall) -> ServiceResponse:
        return await _async_mutate(entry, call, "add")

    async def async_consume(call: ServiceCall) -> ServiceResponse:
        return await _async_mutate(entry, call, "consume")

    async def async_confirm_product(call: ServiceCall) -> ServiceResponse:
        return await _async_confirm_product(entry, call)

    async def async_confirm_product_transaction(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_confirm_product_transaction(entry, call)

    async def async_resolve_product_phrase(call: ServiceCall) -> ServiceResponse:
        return await _async_resolve_product_phrase(entry, call)

    async def async_voice_transaction(call: ServiceCall) -> ServiceResponse:
        return await _async_voice_transaction(entry, call)

    async def async_confirm_voice_transaction(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_confirm_voice_transaction(entry, call)

    async def async_learn_product_alias(call: ServiceCall) -> ServiceResponse:
        return await _async_learn_product_alias(entry, call)

    async def async_remove_product_alias(call: ServiceCall) -> ServiceResponse:
        return await _async_remove_product_alias(entry, call)

    async def async_list_product_aliases(_call: ServiceCall) -> ServiceResponse:
        return await _async_list_product_aliases(entry)

    async def async_merge_products(call: ServiceCall) -> ServiceResponse:
        return await _async_merge_products(entry, call)

    async def async_undo_transaction(call: ServiceCall) -> ServiceResponse:
        return await _async_undo_transaction(entry, call)

    async def async_acknowledge_reconciliation(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_acknowledge_reconciliation(entry, call)

    async def async_start_product_identification(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_start_product_identification(entry, call)

    async def async_confirm_product_identification(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_confirm_product_identification(entry, call)

    async def async_override_product_identification(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_override_product_identification(entry, call)

    async def async_complete_product_identification(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_complete_product_identification(entry, call)

    async def async_reject_product_identification(
        call: ServiceCall,
    ) -> ServiceResponse:
        return await _async_reject_product_identification(entry, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOOKUP,
        async_lookup,
        schema=LOOKUP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESEARCH_BARCODE,
        async_research_barcode,
        schema=RESEARCH_BARCODE_SCHEMA,
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
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIRM_PRODUCT_TRANSACTION,
        async_confirm_product_transaction,
        schema=CONFIRM_PRODUCT_TRANSACTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESOLVE_PRODUCT_PHRASE,
        async_resolve_product_phrase,
        schema=RESOLVE_PRODUCT_PHRASE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VOICE_TRANSACTION,
        async_voice_transaction,
        schema=VOICE_TRANSACTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIRM_VOICE_TRANSACTION,
        async_confirm_voice_transaction,
        schema=CONFIRM_VOICE_TRANSACTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LEARN_PRODUCT_ALIAS,
        async_learn_product_alias,
        schema=PRODUCT_ALIAS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_PRODUCT_ALIAS,
        async_remove_product_alias,
        schema=REMOVE_PRODUCT_ALIAS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_PRODUCT_ALIASES,
        async_list_product_aliases,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MERGE_PRODUCTS,
        async_merge_products,
        schema=MERGE_PRODUCTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UNDO_TRANSACTION,
        async_undo_transaction,
        schema=UNDO_TRANSACTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACKNOWLEDGE_RECONCILIATION,
        async_acknowledge_reconciliation,
        schema=ACKNOWLEDGE_RECONCILIATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_PRODUCT_IDENTIFICATION,
        async_start_product_identification,
        schema=START_PRODUCT_IDENTIFICATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIRM_PRODUCT_IDENTIFICATION,
        async_confirm_product_identification,
        schema=CONFIRM_PRODUCT_IDENTIFICATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_OVERRIDE_PRODUCT_IDENTIFICATION,
        async_override_product_identification,
        schema=OVERRIDE_PRODUCT_IDENTIFICATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_COMPLETE_PRODUCT_IDENTIFICATION,
        async_complete_product_identification,
        schema=COMPLETE_PRODUCT_IDENTIFICATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REJECT_PRODUCT_IDENTIFICATION,
        async_reject_product_identification,
        schema=IDENTIFICATION_JOB_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Grocy Stock Manager actions."""
    hass.services.async_remove(DOMAIN, SERVICE_LOOKUP)
    hass.services.async_remove(DOMAIN, SERVICE_RESEARCH_BARCODE)
    hass.services.async_remove(DOMAIN, SERVICE_ADD)
    hass.services.async_remove(DOMAIN, SERVICE_CONSUME)
    hass.services.async_remove(DOMAIN, SERVICE_CONFIRM_PRODUCT)
    hass.services.async_remove(DOMAIN, SERVICE_CONFIRM_PRODUCT_TRANSACTION)
    hass.services.async_remove(DOMAIN, SERVICE_RESOLVE_PRODUCT_PHRASE)
    hass.services.async_remove(DOMAIN, SERVICE_VOICE_TRANSACTION)
    hass.services.async_remove(DOMAIN, SERVICE_CONFIRM_VOICE_TRANSACTION)
    hass.services.async_remove(DOMAIN, SERVICE_LEARN_PRODUCT_ALIAS)
    hass.services.async_remove(DOMAIN, SERVICE_REMOVE_PRODUCT_ALIAS)
    hass.services.async_remove(DOMAIN, SERVICE_LIST_PRODUCT_ALIASES)
    hass.services.async_remove(DOMAIN, SERVICE_MERGE_PRODUCTS)
    hass.services.async_remove(DOMAIN, SERVICE_UNDO_TRANSACTION)
    hass.services.async_remove(DOMAIN, SERVICE_ACKNOWLEDGE_RECONCILIATION)
    hass.services.async_remove(DOMAIN, SERVICE_START_PRODUCT_IDENTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_CONFIRM_PRODUCT_IDENTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_OVERRIDE_PRODUCT_IDENTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_COMPLETE_PRODUCT_IDENTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_REJECT_PRODUCT_IDENTIFICATION)

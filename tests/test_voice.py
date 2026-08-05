"""Tests for Grocy-backed, fail-closed voice product resolution."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from custom_components.grocy_stock_manager.api import (
    GrocyCannotConnectError,
    GrocyInvalidResponseError,
    GrocyMutationOutcomeUnknownError,
)
from custom_components.grocy_stock_manager.models import (
    Location,
    ProductDetails,
    ProductLookupResult,
    QuantityUnit,
    StockLocation,
)
from custom_components.grocy_stock_manager.voice import (
    GrocyVoiceAliases,
    GrocyVoiceManager,
    GrocyVoiceResolver,
    VoiceAliasConflictError,
    VoiceCandidateNotAllowedError,
    normalise_product_phrase,
)


def _product(
    product_id: int,
    name: str,
    *,
    aliases: str | None = None,
) -> dict[str, object]:
    return {
        "id": str(product_id),
        "name": name,
        "active": "1",
        "userfields": {"voice_aliases": aliases},
    }


def _lookup(
    product_id: int,
    name: str,
    *,
    stock: str = "1",
) -> ProductLookupResult:
    location = Location(12, "Garage L1")
    amount = Decimal(stock)
    return ProductLookupResult(
        lookup_type="product_id",
        lookup_value=product_id,
        product=ProductDetails(
            id=product_id,
            name=name,
            barcodes=(),
            quantity_unit=QuantityUnit(1, "Pack", "Packs"),
            stock_total=amount,
            default_location=location,
            default_consume_location_id=None,
        ),
        stock_locations=(StockLocation(12, "Garage L1", amount),),
    )


def test_normalise_product_phrase_handles_speech_punctuation() -> None:
    """Speech variants have one stable comparison form."""
    assert normalise_product_phrase("  Got2B's—Hair GEL! ") == "got2b s hair gel"


async def test_learn_alias_merges_and_verifies_in_grocy() -> None:
    """Learning preserves existing aliases and writes only the alias userfield."""
    client = AsyncMock()
    client.async_get_products.return_value = [
        _product(7, "got2b glued Styling Gel", aliases='["styling gel"]')
    ]
    client.async_get_product_userfields.side_effect = [
        {"voice_aliases": "styling gel", "owner": "Ben"},
        {"voice_aliases": "hair gel\nstyling gel", "owner": "Ben"},
    ]
    aliases = GrocyVoiceAliases(client)

    assert await aliases.async_learn("Hair gel", 7) is True
    client.async_set_product_userfield.assert_awaited_once_with(
        7,
        "voice_aliases",
        "hair gel\nstyling gel",
    )


async def test_aliases_accept_lines_commas_and_legacy_json() -> None:
    """The Grocy field is convenient to edit while old JSON remains readable."""
    client = AsyncMock()
    aliases = GrocyVoiceAliases(client)

    client.async_get_products.return_value = [
        _product(7, "Styling gel", aliases="hair gel\ngot2b gel, styling gel")
    ]
    readable = await aliases.async_list()
    assert [item["product_phrase"] for item in readable] == [
        "got2b gel",
        "hair gel",
        "styling gel",
    ]

    client.async_get_products.return_value = [
        _product(7, "Styling gel", aliases='["hair gel","styling gel"]')
    ]
    legacy = await aliases.async_list()
    assert [item["product_phrase"] for item in legacy] == [
        "hair gel",
        "styling gel",
    ]


async def test_learn_alias_rejects_reassignment() -> None:
    """The same spoken phrase can never silently point at another product."""
    client = AsyncMock()
    client.async_get_products.return_value = [
        _product(7, "Styling gel", aliases='["hair gel"]'),
        _product(8, "Shower gel"),
    ]
    aliases = GrocyVoiceAliases(client)

    with pytest.raises(VoiceAliasConflictError):
        await aliases.async_learn("Hair gel", 8)
    client.async_set_product_userfield.assert_not_awaited()


async def test_learn_alias_verification_failure_is_outcome_unknown() -> None:
    """A failed read-back never reports an alias write as safely failed."""
    client = AsyncMock()
    client.async_get_products.return_value = [_product(7, "Styling gel")]
    client.async_get_product_userfields.side_effect = [
        {"voice_aliases": None},
        GrocyCannotConnectError(),
    ]
    aliases = GrocyVoiceAliases(client)

    with pytest.raises(GrocyMutationOutcomeUnknownError):
        await aliases.async_learn("Hair gel", 7)


async def test_malformed_alias_data_fails_closed() -> None:
    """Manual corruption in Grocy never becomes a guessed product mapping."""
    client = AsyncMock()
    client.async_get_products.return_value = [
        _product(7, "Styling gel", aliases='["hair gel"')
    ]
    aliases = GrocyVoiceAliases(client)

    with pytest.raises(GrocyInvalidResponseError):
        await aliases.async_list()


async def test_unique_grocy_alias_is_authoritative() -> None:
    """A unique learned alias can resolve without asking again."""
    client = AsyncMock()
    client.async_get_products.return_value = [
        _product(7, "got2b glued Styling Gel", aliases='["hair gel"]')
    ]
    product_resolver = AsyncMock()
    product_resolver.async_lookup_by_product_id.return_value = _lookup(
        7, "got2b glued Styling Gel"
    )
    resolver = GrocyVoiceResolver(
        client,
        product_resolver,
        GrocyVoiceAliases(client),
    )

    result = await resolver.async_resolve("hair gel", operation="consume")

    assert result.status == "resolved"
    assert result.candidates[0].match_type == "learned_alias"
    assert result.candidates[0].lookup.product.id == 7


async def test_duplicate_alias_is_ambiguous_and_never_authoritative() -> None:
    """Conflicting Grocy aliases are exposed for correction, not guessed."""
    client = AsyncMock()
    client.async_get_products.return_value = [
        _product(7, "Styling gel", aliases='["gel"]'),
        _product(8, "Shower gel", aliases='["gel"]'),
    ]
    product_resolver = AsyncMock()
    product_resolver.async_lookup_by_product_id.side_effect = [
        _lookup(7, "Styling gel"),
        _lookup(8, "Shower gel"),
    ]
    resolver = GrocyVoiceResolver(
        client,
        product_resolver,
        GrocyVoiceAliases(client),
    )

    result = await resolver.async_resolve("gel", operation="consume")

    assert result.status == "ambiguous"
    assert len(result.candidates) == 2


async def test_similar_product_requires_confirmation_before_stock_write() -> None:
    """Hair gel is suggested for a branded name but cannot auto-consume."""
    client = AsyncMock()
    client.async_get_products.return_value = [
        _product(7, "got2b glued Styling Gel")
    ]
    product_resolver = AsyncMock()
    product_resolver.async_lookup_by_product_id.return_value = _lookup(
        7, "got2b glued Styling Gel"
    )
    resolver = GrocyVoiceResolver(
        client,
        product_resolver,
        GrocyVoiceAliases(client),
    )
    transactions = AsyncMock()
    manager = GrocyVoiceManager(
        resolver,
        product_resolver,
        transactions,
        GrocyVoiceAliases(client),
    )

    response = await manager.async_process(
        operation="consume",
        product_phrase="hair gel",
        amount=Decimal("1"),
        request_id="voice-test-1",
        location_id=None,
        location_name=None,
        source="garage_voice",
        candidate_limit=3,
    )

    assert response["status"] == "needs_confirmation"
    assert response["stock_changed"] is False
    assert response["candidates"][0]["product_id"] == 7
    assert "confirmation_token" in response
    transactions.async_execute.assert_not_awaited()


async def test_confirmation_rejects_unoffered_product_without_using_token() -> None:
    """A bad tablet selection cannot redirect a staged transaction."""
    client = AsyncMock()
    client.async_get_products.return_value = [_product(7, "Styling Gel")]
    product_resolver = AsyncMock()
    product_resolver.async_lookup_by_product_id.return_value = _lookup(
        7, "Styling Gel"
    )
    resolver = GrocyVoiceResolver(
        client,
        product_resolver,
        GrocyVoiceAliases(client),
    )
    transactions = AsyncMock()
    manager = GrocyVoiceManager(
        resolver,
        product_resolver,
        transactions,
        GrocyVoiceAliases(client),
    )
    staged = await manager.async_process(
        operation="consume",
        product_phrase="hair gel",
        amount=Decimal("1"),
        request_id="voice-test-2",
        location_id=None,
        location_name=None,
        source="garage_voice",
        candidate_limit=3,
    )

    with pytest.raises(VoiceCandidateNotAllowedError):
        await manager.async_confirm(
            confirmation_token=staged["confirmation_token"],
            product_id=999,
            learn_alias=False,
        )

    transactions.async_execute.return_value = {"outcome": "committed"}
    confirmed = await manager.async_confirm(
        confirmation_token=staged["confirmation_token"],
        product_id=7,
        learn_alias=False,
    )
    assert confirmed["status"] == "committed"
    transactions.async_execute.assert_awaited_once()

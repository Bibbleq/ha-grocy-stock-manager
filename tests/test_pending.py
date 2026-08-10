"""Tests for durable voice confirmation intents."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from custom_components.grocy_stock_manager.pending import (
    PendingVoiceTransaction,
    VoiceConfirmationStore,
)


def _pending(created_at: float = 1_800_000_000.0) -> PendingVoiceTransaction:
    return PendingVoiceTransaction(
        created_at=created_at,
        operation="consume",
        product_phrase="hair gel",
        amount=Decimal("1"),
        request_id="voice-1",
        location_id=None,
        location_name=None,
        source="garage_voice",
        candidate_ids=frozenset({7, 8}),
    )


async def test_pending_confirmation_survives_store_reload() -> None:
    """A restart preserves the captured operation and offered candidates."""
    storage = AsyncMock()
    storage.async_load.return_value = {
        "records": {"token-1": _pending().as_storage_dict()}
    }
    store = object.__new__(VoiceConfirmationStore)
    store._store = storage
    store._records = {}
    store._lock = asyncio.Lock()

    with patch.object(store, "now_timestamp", return_value=1_800_000_030.0):
        await store.async_load()
        assert store.snapshot()[0]["request_id"] == "voice-1"
        assert await store.async_take("token-1", 999) == _pending()
        restored = await store.async_take("token-1", 7)

    assert restored is not None
    assert restored.operation == "consume"
    assert restored.candidate_ids == frozenset({7, 8})
    assert await store.async_take("token-1", 7) is None


async def test_expired_confirmation_is_not_restored() -> None:
    """Expired speech cannot be confirmed after a long HA outage."""
    storage = AsyncMock()
    storage.async_load.return_value = {
        "records": {"expired": _pending(created_at=1_700_000_000.0).as_storage_dict()}
    }
    store = object.__new__(VoiceConfirmationStore)
    store._store = storage
    store._records = {}
    store._lock = asyncio.Lock()

    with patch.object(store, "now_timestamp", return_value=1_800_000_000.0):
        await store.async_load()

    assert store.snapshot() == []
    assert await store.async_take("expired", 7) is None

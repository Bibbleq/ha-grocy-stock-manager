"""Tests for the Grocy Stock Manager config flow."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.grocy_stock_manager.api import GrocyInvalidAuthError
from custom_components.grocy_stock_manager.const import DOMAIN

USER_INPUT = {
    CONF_URL: "http://grocy.local:9192/api/",
    CONF_API_KEY: "secret",
    CONF_VERIFY_SSL: False,
}


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """A valid Grocy endpoint creates a canonical config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.grocy_stock_manager.api.GrocyApiClient."
        "async_get_system_info",
        AsyncMock(return_value={"grocy_version": {"Version": "4.6.0"}}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "http://grocy.local:9192"
    assert result["data"] == {
        CONF_URL: "http://grocy.local:9192",
        CONF_API_KEY: "secret",
        CONF_VERIFY_SSL: False,
    }


async def test_user_flow_rejects_invalid_auth(hass: HomeAssistant) -> None:
    """An invalid API key leaves the user on the form with a clear error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    with patch(
        "custom_components.grocy_stock_manager.api.GrocyApiClient."
        "async_get_system_info",
        AsyncMock(side_effect=GrocyInvalidAuthError),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

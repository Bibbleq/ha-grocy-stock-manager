"""Config flow for Grocy Stock Manager."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    GrocyApiClient,
    GrocyApiError,
    GrocyCannotConnectError,
    GrocyInvalidAuthError,
    GrocyInvalidResponseError,
    normalise_base_url,
)
from .const import DEFAULT_VERIFY_SSL, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): BooleanSelector(),
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


class GrocyStockManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Grocy Stock Manager."""

    VERSION = 1

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, validated_data = await self._async_validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=validated_data[CONF_URL],
                    data=validated_data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=CONFIG_SCHEMA,
            errors=errors,
        )

    @override
    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start API-key reauthentication."""
        self.reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store a replacement API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            updated_data = {
                **self.reauth_entry.data,
                CONF_API_KEY: user_input[CONF_API_KEY],
            }
            errors, validated_data = await self._async_validate(updated_data)
            if not errors:
                return self.async_update_reload_and_abort(
                    self.reauth_entry,
                    data=validated_data,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )

    @override
    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Update connection settings and reload the integration."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            errors, validated_data = await self._async_validate(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data=validated_data,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CONFIG_SCHEMA,
                reconfigure_entry.data,
            ),
            errors=errors,
        )

    async def _async_validate(
        self,
        user_input: Mapping[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Validate and canonicalise connection data."""
        errors: dict[str, str] = {}
        validated_data = dict(user_input)

        try:
            validated_data[CONF_URL] = normalise_base_url(str(validated_data[CONF_URL]))
        except ValueError:
            errors["base"] = "invalid_url"
            return errors, validated_data

        session = async_get_clientsession(
            self.hass,
            verify_ssl=bool(validated_data[CONF_VERIFY_SSL]),
        )
        client = GrocyApiClient(
            session,
            validated_data[CONF_URL],
            str(validated_data[CONF_API_KEY]),
        )

        try:
            await client.async_get_system_info()
        except GrocyInvalidAuthError:
            errors["base"] = "invalid_auth"
        except GrocyCannotConnectError:
            errors["base"] = "cannot_connect"
        except GrocyInvalidResponseError, GrocyApiError:
            errors["base"] = "invalid_response"
        except Exception:  # pragma: no cover - defensive boundary
            _LOGGER.exception("Unexpected exception while validating Grocy")
            errors["base"] = "unknown"

        return errors, validated_data

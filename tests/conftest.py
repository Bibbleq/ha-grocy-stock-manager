"""Shared test fixtures for Grocy Stock Manager."""

import sys

import pytest

if sys.platform != "win32":
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Enable loading custom integrations in every Home Assistant test."""
        yield

"""Shared explicit fixtures for bootstrap tests."""

import pytest

from fulfillflow.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Return deterministic test settings without relying on a .env file."""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+psycopg://fulfillflow:test@localhost:5432/fulfillflow_test",
        session_secret="session-test-value",
        carrier_alpha_webhook_secret="alpha-test-value",
        carrier_beta_webhook_secret="beta-test-value",
    )

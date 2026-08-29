"""PostgreSQL migration and real readiness integration test."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.main import create_app


@pytest.mark.integration
def test_migrations_reach_head_and_application_becomes_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL must point to a dedicated PostgreSQL 18 database")

    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        session_secret="integration-session",
        carrier_alpha_webhook_secret="integration-alpha",
        carrier_beta_webhook_secret="integration-beta",
    )

    async def verify_readiness() -> None:
        app = create_app(settings, alembic_config_path=Path("alembic.ini"))
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                live_response = await client.get("/health/live")
                ready_response = await client.get("/health/ready")

        assert live_response.status_code == 200
        assert ready_response.status_code == 200
        assert ready_response.json() == {"status": "ok"}

    run_async(verify_readiness())

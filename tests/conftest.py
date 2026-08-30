"""Shared explicit fixtures, with PostgreSQL opt-in for persistence tests."""

import asyncio
import os
import selectors
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from fulfillflow.config import Settings
from fulfillflow.db import Database
from tests.support import FixedClock


def pytest_asyncio_loop_factories(
    config: object,
    item: object,
) -> dict[str, object]:
    """Run async tests on psycopg-compatible selector loops on Windows."""
    del config, item
    if sys.platform == "win32":
        return {"selector": lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())}
    return {"default": asyncio.new_event_loop}


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


@pytest.fixture
def fixed_clock() -> FixedClock:
    """Return a deterministic clock independent of wall time."""
    return FixedClock(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))


@pytest.fixture
def postgres_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Migrate and configure the explicitly dedicated PostgreSQL test database."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL must point to a dedicated PostgreSQL 18 database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config(str(Path("alembic.ini"))), "head")
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        session_secret="postgres-test-session",
        carrier_alpha_webhook_secret="postgres-test-alpha",
        carrier_beta_webhook_secret="postgres-test-beta",
    )


@pytest.fixture
async def postgres_database(postgres_settings: Settings) -> AsyncIterator[Database]:
    """Yield a real database with business tables emptied in FK-safe order."""
    database = Database.from_settings(postgres_settings)
    await _clear_business_rows(database)
    try:
        yield database
    finally:
        await _clear_business_rows(database)
        await database.dispose()


@pytest.fixture
async def carrier_id(postgres_database: Database) -> UUID:
    """Return the deterministic Alpha reference Carrier installed by Alembic."""
    identifier = UUID("00000000-0000-4000-8000-000000000100")
    async with postgres_database.session() as session:
        code = await session.scalar(
            text("SELECT code FROM carriers WHERE id = :id"),
            {"id": identifier},
        )
    assert code == "carrier-alpha"
    return identifier


async def _clear_business_rows(database: Database) -> None:
    async with database.engine.begin() as connection:
        await connection.execute(text("DELETE FROM tracking_events"))
        await connection.execute(text("DELETE FROM carrier_event_inbox"))
        await connection.execute(text("DELETE FROM shipments"))
        await connection.execute(text("DELETE FROM orders"))
        await connection.execute(
            text(
                "DELETE FROM carriers WHERE id NOT IN "
                "(CAST('00000000-0000-4000-8000-000000000100' AS uuid), "
                "CAST('00000000-0000-4000-8000-000000000101' AS uuid))"
            )
        )

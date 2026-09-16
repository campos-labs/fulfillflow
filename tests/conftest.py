"""Shared explicit fixtures, with PostgreSQL opt-in for persistence tests."""

import asyncio
import os
import selectors
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import aio_pika
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.messaging.amqp import declare_flow
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


@pytest.fixture(autouse=True)
def internal_test_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "INTERNAL_API_SECRET", "76a9e82ae9ca4a81b9ea9e13d4e4af77301827de7a1240419899cba88bb13e53"
    )


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
    command.upgrade(Config(str(Path("alembic_core.ini"))), "head")
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        session_secret="postgres-test-session",
        carrier_alpha_webhook_secret="postgres-test-alpha",
        carrier_beta_webhook_secret="postgres-test-beta",
    )


@pytest.fixture
async def postgres_database(
    postgres_settings: Settings, postgres_tracking_database: Database
) -> AsyncIterator[Database]:
    """Yield a real database with business tables emptied in FK-safe order."""
    database = Database.from_settings(postgres_settings)
    await _clear_business_rows(database)
    # Tests own this vhost. A prior crash may leave confirmed transport duplicates
    # after its database fixture was cleared; each case starts with empty flows.
    if url := os.environ.get("TEST_AMQP_URL"):
        connection = await aio_pika.connect(url, timeout=10)
        async with connection:
            channel = await connection.channel()
            for flow in ("tracking.apply.v1", "tracking.result.v1"):
                await declare_flow(channel, flow)
                queue = await channel.get_queue(f"{flow}.queue")
                await queue.purge()
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
        for table in ("message_inbox", "message_outbox", "message_quarantine", "message_rearm"):
            await connection.execute(text(f"DELETE FROM {table}"))
        await connection.execute(text("DELETE FROM notifications"))
        await connection.execute(text("DELETE FROM tracking_event_receipts"))
        await connection.execute(text("DELETE FROM shipments"))
        await connection.execute(text("DELETE FROM orders"))
        await connection.execute(
            text(
                "DELETE FROM carriers WHERE id NOT IN "
                "(CAST('00000000-0000-4000-8000-000000000100' AS uuid), "
                "CAST('00000000-0000-4000-8000-000000000101' AS uuid))"
            )
        )


@pytest.fixture
def postgres_tracking_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    url = os.environ.get("TEST_TRACKING_DATABASE_URL")
    if url is None:
        pytest.skip("TEST_TRACKING_DATABASE_URL must point to a dedicated PostgreSQL 18 database")
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic_tracking.ini"), "head")
    return Settings(
        _env_file=None,
        app_env="test",
        service_role="tracking",
        database_url=url,
        carrier_alpha_webhook_secret="postgres-test-alpha",
        carrier_beta_webhook_secret="postgres-test-beta",
    )


@pytest.fixture
async def postgres_tracking_database(
    postgres_tracking_settings: Settings,
) -> AsyncIterator[Database]:
    database = Database.from_settings(postgres_tracking_settings)

    async def clear() -> None:
        async with database.engine.begin() as connection:
            for table in ("message_inbox", "message_outbox", "message_quarantine", "message_rearm"):
                await connection.execute(text(f"DELETE FROM {table}"))
            await connection.execute(text("DELETE FROM tracking_events"))
            await connection.execute(text("DELETE FROM carrier_event_inbox"))

    await clear()
    try:
        yield database
    finally:
        await clear()
        await database.dispose()


@pytest.fixture
def legacy_postgres_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    url = os.environ.get("TEST_LEGACY_DATABASE_URL")
    if url is None:
        pytest.skip("TEST_LEGACY_DATABASE_URL must point to a dedicated PostgreSQL 18 database")
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    return Settings(_env_file=None, app_env="test", database_url=url, session_secret="legacy-test")


@pytest.fixture
async def legacy_postgres_database(legacy_postgres_settings: Settings) -> AsyncIterator[Database]:
    database = Database.from_settings(legacy_postgres_settings)

    async def clear() -> None:
        async with database.engine.begin() as connection:
            for name in (
                "notifications",
                "tracking_events",
                "carrier_event_inbox",
                "shipments",
                "orders",
            ):
                await connection.execute(text(f"DELETE FROM {name}"))

    await clear()
    try:
        yield database
    finally:
        await clear()
        await database.dispose()

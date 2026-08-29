"""FastAPI health endpoint contract tests."""

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.db.migrations import SchemaNotCurrentError
from fulfillflow.main import create_app


class _FakeDatabase:
    def __init__(self) -> None:
        self.engine = object()
        self.ping_calls = 0
        self.disposed = False
        self.ping_error: SQLAlchemyError | None = None

    async def ping(self) -> None:
        self.ping_calls += 1
        if self.ping_error is not None:
            raise self.ping_error

    async def dispose(self) -> None:
        self.disposed = True


def _client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


async def test_liveness_does_not_access_database(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    database = _FakeDatabase()
    schema_check = AsyncMock(return_value=True)
    monkeypatch.setattr("fulfillflow.main.schema_is_current", schema_check)
    app = create_app(settings, cast(Database, database), Path("alembic.ini"))

    async with app.router.lifespan_context(app):
        async with _client_for(app) as client:
            response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert database.ping_calls == 0
    schema_check.assert_awaited_once()
    assert database.disposed is True


async def test_readiness_is_unavailable_before_startup_gate(settings: Settings) -> None:
    app = create_app(settings)

    async with _client_for(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_readiness_pings_database_after_schema_gate(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    database = _FakeDatabase()
    monkeypatch.setattr("fulfillflow.main.schema_is_current", AsyncMock(return_value=True))
    app = create_app(settings, cast(Database, database))

    async with app.router.lifespan_context(app):
        async with _client_for(app) as client:
            response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert database.ping_calls == 1


async def test_readiness_hides_database_failures(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    database = _FakeDatabase()
    database.ping_error = SQLAlchemyError("sensitive database failure")
    monkeypatch.setattr("fulfillflow.main.schema_is_current", AsyncMock(return_value=True))
    app = create_app(settings, cast(Database, database))

    async with app.router.lifespan_context(app):
        async with _client_for(app) as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "sensitive" not in response.text


async def test_startup_fails_when_schema_is_not_current(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    database = _FakeDatabase()
    monkeypatch.setattr("fulfillflow.main.schema_is_current", AsyncMock(return_value=False))
    app = create_app(settings, cast(Database, database))

    with pytest.raises(SchemaNotCurrentError, match="does not match"):
        async with app.router.lifespan_context(app):
            pass

    assert app.state.schema_ready is False
    assert database.disposed is True

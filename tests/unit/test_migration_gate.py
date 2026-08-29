"""Unit tests for the one-time Alembic schema gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fulfillflow.db import migrations


class _FakeConnection:
    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def run_sync(self, function: Any) -> frozenset[str]:
        return function(object())


class _FakeEngine:
    def connect(self) -> _FakeConnection:
        return _FakeConnection()


class _ScriptDirectory:
    def __init__(self, heads: tuple[str, ...]) -> None:
        self._heads = heads

    def get_heads(self) -> list[str]:
        return list(self._heads)


class _MigrationContext:
    def __init__(self, heads: tuple[str, ...]) -> None:
        self._heads = heads

    def get_current_heads(self) -> tuple[str, ...]:
        return self._heads


@pytest.mark.parametrize(
    ("script_heads", "database_heads", "expected"),
    [
        (("0001_bootstrap",), ("0001_bootstrap",), True),
        (("0001_bootstrap",), (), False),
        (("0001_bootstrap",), ("older",), False),
    ],
)
async def test_schema_gate_compares_exact_head_sets(
    monkeypatch: pytest.MonkeyPatch,
    script_heads: tuple[str, ...],
    database_heads: tuple[str, ...],
    expected: bool,
) -> None:
    monkeypatch.setattr(
        migrations.ScriptDirectory,
        "from_config",
        lambda _config: _ScriptDirectory(script_heads),
    )
    monkeypatch.setattr(
        migrations.MigrationContext,
        "configure",
        lambda _connection: _MigrationContext(database_heads),
    )

    result = await migrations.schema_is_current(
        cast(AsyncEngine, _FakeEngine()),
        Path("alembic.ini"),
    )

    assert result is expected


async def test_schema_gate_rejects_repository_without_a_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migrations.ScriptDirectory,
        "from_config",
        lambda _config: _ScriptDirectory(()),
    )

    with pytest.raises(migrations.SchemaConfigurationError, match="no head"):
        await migrations.schema_is_current(
            cast(AsyncEngine, _FakeEngine()),
            Path("alembic.ini"),
        )

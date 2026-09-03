"""Static and invocation-level safety gates for seed scripts."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import sqlalchemy as sa
from benchmarks import seed_loader
from benchmarks.seed_loader import SeedSafetyError


def test_seed_loader_contains_no_destructive_sql() -> None:
    source = inspect.getsource(seed_loader).upper()

    for forbidden in ("DELETE FROM", "TRUNCATE", "DROP TABLE", "DOWNGRADE"):
        assert forbidden not in source


@pytest.mark.parametrize("app_env", ["production", "benchmark", ""])
def test_demo_seed_rejects_inappropriate_app_env(app_env: str) -> None:
    with pytest.raises(SeedSafetyError, match="APP_ENV"):
        seed_loader._validate_invocation(
            "demo",
            database_url="postgresql+psycopg://user:password@localhost/demo",
            app_env=app_env,
            confirmed_database_name="demo",
        )


def test_seed_requires_psycopg_and_literal_database_confirmation() -> None:
    with pytest.raises(SeedSafetyError, match=r"postgresql\+psycopg"):
        seed_loader._validate_invocation(
            "benchmark",
            database_url="sqlite:///benchmark.db",
            app_env="benchmark",
            confirmed_database_name="benchmark",
        )
    with pytest.raises(SeedSafetyError, match="literal database confirmation"):
        seed_loader._validate_invocation(
            "benchmark",
            database_url="postgresql+psycopg://user:password@localhost/benchmark",
            app_env="benchmark",
            confirmed_database_name="another_database",
        )


class _FakeConnection:
    def __init__(self, scalars: list[object], heads: frozenset[str] = frozenset()) -> None:
        self.scalars = iter(scalars)
        self.heads = heads

    async def scalar(self, _statement: object) -> object:
        return next(self.scalars)

    async def run_sync(self, _callable: object) -> frozenset[str]:
        return self.heads


class _FakeScriptDirectory:
    def __init__(self, heads: list[str]) -> None:
        self.heads = heads

    def get_heads(self) -> list[str]:
        return self.heads


async def test_connected_database_must_match_literal_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seed_loader.ScriptDirectory,
        "from_config",
        lambda _config: _FakeScriptDirectory(["head"]),
    )
    connection = _FakeConnection(["another_database"])

    with pytest.raises(SeedSafetyError, match="connected database"):
        await seed_loader._validate_postgresql_target(
            connection,  # type: ignore[arg-type]
            confirmed_database_name="confirmed_database",
            alembic_config_path=Path("alembic.ini"),
        )


async def test_postgresql_major_decision_rejects_non_18(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seed_loader.ScriptDirectory,
        "from_config",
        lambda _config: _FakeScriptDirectory(["head"]),
    )
    connection = _FakeConnection(["benchmark", 170_000])

    with pytest.raises(SeedSafetyError, match="major version 18"):
        await seed_loader._validate_postgresql_target(
            connection,  # type: ignore[arg-type]
            confirmed_database_name="benchmark",
            alembic_config_path=Path("alembic.ini"),
        )


@pytest.mark.parametrize(
    ("repository_heads", "database_heads", "message"),
    [([], frozenset(), "no head"), (["expected"], frozenset(), "does not match")],
)
async def test_alembic_head_absent_or_incorrect_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    repository_heads: list[str],
    database_heads: frozenset[str],
    message: str,
) -> None:
    monkeypatch.setattr(
        seed_loader.ScriptDirectory,
        "from_config",
        lambda _config: _FakeScriptDirectory(repository_heads),
    )
    connection = _FakeConnection(["benchmark", 180_000], database_heads)

    with pytest.raises(SeedSafetyError, match=message):
        await seed_loader._validate_postgresql_target(
            connection,  # type: ignore[arg-type]
            confirmed_database_name="benchmark",
            alembic_config_path=Path("alembic.ini"),
        )


def test_reflected_schema_with_missing_required_column_is_rejected() -> None:
    metadata = sa.MetaData()
    for name in seed_loader._REFLECTED_TABLES:
        sa.Table(name, metadata, sa.Column("id", sa.Uuid(), primary_key=True))

    with pytest.raises(SeedSafetyError, match="missing required columns"):
        seed_loader._validate_reflected_schema(metadata)

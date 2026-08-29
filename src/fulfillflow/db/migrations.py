"""Alembic schema compatibility checks used during application startup."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine


class SchemaConfigurationError(RuntimeError):
    """Raised when the repository has no usable Alembic head."""


class SchemaNotCurrentError(RuntimeError):
    """Raised when the database is not at the repository's Alembic head."""


def _current_heads(connection: Connection) -> frozenset[str]:
    context = MigrationContext.configure(connection)
    return frozenset(context.get_current_heads())


async def schema_is_current(engine: AsyncEngine, config_path: Path) -> bool:
    """Compare database heads with script heads using one startup connection."""
    alembic_config = Config(str(config_path))
    expected_heads = frozenset(ScriptDirectory.from_config(alembic_config).get_heads())
    if not expected_heads:
        raise SchemaConfigurationError("the Alembic script directory has no head")

    async with engine.connect() as connection:
        database_heads = await connection.run_sync(_current_heads)
    return database_heads == expected_heads

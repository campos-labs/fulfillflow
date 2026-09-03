"""Fail-closed atomic PostgreSQL loader for deterministic logical datasets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from benchmarks.database_contract import BUSINESS_TABLES, database_digests
from benchmarks.dataset import DatasetName, LogicalDataset, generate_dataset

_TABLE_ORDER = BUSINESS_TABLES
_REFLECTED_TABLES = ("carriers", *_TABLE_ORDER)
_ALLOWED_ENVIRONMENTS: dict[DatasetName, frozenset[str]] = {
    "demo": frozenset({"local", "test"}),
    "benchmark": frozenset({"benchmark", "test"}),
}
_OFFICIAL_CARRIERS = {
    "carrier-alpha": {
        "id": UUID("00000000-0000-4000-8000-000000000100"),
        "name": "Carrier Alpha",
        "adapter_key": "alpha",
        "active": True,
    },
    "carrier-beta": {
        "id": UUID("00000000-0000-4000-8000-000000000101"),
        "name": "Carrier Beta",
        "adapter_key": "beta",
        "active": True,
    },
}


class SeedSafetyError(RuntimeError):
    """Raised before commit when a seed safety invariant is not satisfied."""


@dataclass(frozen=True, slots=True)
class SeedLoadResult:
    """Sanitized seed outcome safe to print without connection information."""

    dataset: DatasetName
    database_name: str
    logical_hash: str
    counts: dict[str, int]
    inserted: bool


async def load_dataset(
    name: DatasetName,
    *,
    database_url: str,
    app_env: str,
    confirmed_database_name: str,
    alembic_config_path: Path = Path("alembic.ini"),
) -> SeedLoadResult:
    """Validate the target and insert the exact dataset in one transaction."""
    _validate_invocation(
        name,
        database_url=database_url,
        app_env=app_env,
        confirmed_database_name=confirmed_database_name,
    )
    dataset = generate_dataset(name)
    engine = create_async_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"options": "-c statement_timeout=30000"},
    )
    inserted = False
    try:
        async with engine.begin() as connection:
            await _validate_postgresql_target(
                connection,
                confirmed_database_name=confirmed_database_name,
                alembic_config_path=alembic_config_path,
            )
            metadata = await connection.run_sync(_reflect_schema)
            _validate_reflected_schema(metadata)
            await _validate_official_carriers(connection, metadata.tables["carriers"])
            existing = await _read_business_rows(connection, metadata)
            if all(not rows for rows in existing.values()):
                await _insert_dataset(connection, metadata, dataset)
                inserted = True
            elif _rows_digest(existing) != _rows_digest(dataset.database_rows()):
                raise SeedSafetyError(
                    "database contains divergent or partially populated business data"
                )
    except SeedSafetyError:
        raise
    except Exception:
        # The transaction context has already rolled back. Do not let SQLAlchemy render
        # statement parameters because seed rows include authenticated raw bodies.
        raise SeedSafetyError(
            "seed operation failed safely; the transaction was not committed"
        ) from None
    finally:
        await engine.dispose()

    return SeedLoadResult(
        dataset=name,
        database_name=confirmed_database_name,
        logical_hash=dataset.logical_hash(),
        counts=dataset.counts,
        inserted=inserted,
    )


def _validate_invocation(
    name: DatasetName,
    *,
    database_url: str,
    app_env: str,
    confirmed_database_name: str,
) -> None:
    if app_env not in _ALLOWED_ENVIRONMENTS[name]:
        allowed = ", ".join(sorted(_ALLOWED_ENVIRONMENTS[name]))
        raise SeedSafetyError(f"APP_ENV for {name} seed must be one of: {allowed}")
    try:
        url = make_url(database_url)
    except sa.exc.ArgumentError as exc:
        raise SeedSafetyError("DATABASE_URL is not a valid SQLAlchemy URL") from exc
    if url.drivername != "postgresql+psycopg":
        raise SeedSafetyError("DATABASE_URL must use PostgreSQL through postgresql+psycopg")
    if not url.database:
        raise SeedSafetyError("DATABASE_URL must name a PostgreSQL database")
    if not confirmed_database_name or confirmed_database_name != url.database:
        raise SeedSafetyError(
            "literal database confirmation must exactly match the DATABASE_URL database name"
        )


async def _validate_postgresql_target(
    connection: AsyncConnection,
    *,
    confirmed_database_name: str,
    alembic_config_path: Path,
) -> None:
    database_name = await connection.scalar(sa.text("SELECT current_database()"))
    if database_name != confirmed_database_name:
        raise SeedSafetyError("connected database does not match the literal confirmation")
    version_number = await connection.scalar(
        sa.text("SELECT current_setting('server_version_num')::integer")
    )
    if not isinstance(version_number, int) or version_number // 10_000 != 18:
        raise SeedSafetyError("seeds require PostgreSQL major version 18")
    expected_heads = frozenset(
        ScriptDirectory.from_config(Config(str(alembic_config_path))).get_heads()
    )
    if not expected_heads:
        raise SeedSafetyError("repository Alembic configuration has no head")
    current_heads = await connection.run_sync(_current_alembic_heads)
    if current_heads != expected_heads:
        raise SeedSafetyError("database Alembic head does not match the repository head")


def _current_alembic_heads(connection: Connection) -> frozenset[str]:
    return frozenset(MigrationContext.configure(connection).get_current_heads())


def _reflect_schema(connection: Connection) -> sa.MetaData:
    metadata = sa.MetaData()
    metadata.reflect(bind=connection, only=_REFLECTED_TABLES)
    return metadata


def _validate_reflected_schema(metadata: sa.MetaData) -> None:
    missing = set(_REFLECTED_TABLES) - set(metadata.tables)
    if missing:
        raise SeedSafetyError(f"database schema is missing required tables: {sorted(missing)}")
    expected_columns = {
        "orders": {"id", "external_reference", "recipient_email", "status"},
        "shipments": {
            "id",
            "order_id",
            "carrier_id",
            "tracking_code",
            "status",
            "status_occurred_at",
            "status_event_received_at",
            "status_external_event_id",
        },
        "carrier_event_inbox": {
            "id",
            "carrier_id",
            "external_event_id",
            "payload_sha256",
            "raw_body",
            "parsed_payload",
            "status",
        },
        "tracking_events": {
            "id",
            "inbox_event_id",
            "shipment_id",
            "application_result",
        },
        "notifications": {"id", "shipment_id", "tracking_event_id", "status"},
    }
    for table_name, columns in expected_columns.items():
        absent = columns - set(metadata.tables[table_name].columns.keys())
        if absent:
            raise SeedSafetyError(
                f"database table {table_name} is missing required columns: {sorted(absent)}"
            )


async def _validate_official_carriers(
    connection: AsyncConnection,
    table: sa.Table,
) -> None:
    rows = (await connection.execute(sa.select(table))).mappings().all()
    if len(rows) != len(_OFFICIAL_CARRIERS):
        raise SeedSafetyError("carrier registry must contain exactly Alpha and Beta")
    by_code = {str(row["code"]): row for row in rows}
    if set(by_code) != set(_OFFICIAL_CARRIERS):
        raise SeedSafetyError("carrier registry does not match the official Alpha/Beta pair")
    for code, expected in _OFFICIAL_CARRIERS.items():
        actual = by_code[code]
        if any(actual[field] != value for field, value in expected.items()):
            raise SeedSafetyError(f"official Carrier {code} has divergent reference data")


async def _read_business_rows(
    connection: AsyncConnection,
    metadata: sa.MetaData,
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for table_name in _TABLE_ORDER:
        table = metadata.tables[table_name]
        rows = (await connection.execute(sa.select(table))).mappings().all()
        result[table_name] = [dict(row) for row in rows]
    return result


async def _insert_dataset(
    connection: AsyncConnection,
    metadata: sa.MetaData,
    dataset: LogicalDataset,
) -> None:
    rows_by_table = dataset.database_rows()
    for table_name in _TABLE_ORDER:
        rows = rows_by_table[table_name]
        if rows:
            await connection.execute(sa.insert(metadata.tables[table_name]), rows)


def _rows_digest(rows_by_table: dict[str, list[dict[str, object]]]) -> str:
    """Compatibility wrapper around the one shared canonical database identity."""
    return database_digests(rows_by_table).global_sha256

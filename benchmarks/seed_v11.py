"""Distribute the authenticated frozen dataset using independent owner transactions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from benchmarks.artifact import load_authenticated_document
from benchmarks.database_contract import (
    STRUCTURAL_SCHEMA_QUERIES,
    compare_database_content,
    structural_schema_identity,
)
from benchmarks.dataset import generate_dataset
from benchmarks.seed_loader import (
    SeedSafetyError,
    _validate_invocation,
    _validate_official_carriers,
    _validate_postgresql_target,
)
from benchmarks.semantic import validate_semantic_document
from fulfillflow.asyncio_support import run_async

type Owner = Literal["core", "tracking"]
OWNER_TABLES: dict[Owner, tuple[str, ...]] = {
    "core": ("orders", "shipments", "notifications"),
    "tracking": ("carrier_event_inbox", "tracking_events"),
}
OWNER_SCHEMA_TABLES: dict[Owner, tuple[str, ...]] = {
    "core": (
        "alembic_version",
        "carriers",
        "orders",
        "shipments",
        "notifications",
        "tracking_event_receipts",
    ),
    "tracking": ("alembic_version", "carrier_event_inbox", "tracking_events"),
}
FROZEN_DATASET = Path("benchmarks/datasets/benchmark-v1.0.json")
FROZEN_DIGEST = "5897d7441f73fec77d98ff97196aff0becc3f301e45c708febff493d8f4a63bf"


@dataclass(frozen=True)
class OwnerPreparation:
    owner: Owner
    database: str
    schema_sha256: str
    content_sha256: str
    counts: dict[str, int]
    inserted: bool


def frozen_rows() -> dict[str, list[dict[str, object]]]:
    document, digest = load_authenticated_document(FROZEN_DATASET, expected_sha256=FROZEN_DIGEST)
    validate_semantic_document(document)
    dataset = generate_dataset("benchmark")
    if dataset.logical_hash() != digest:
        raise SeedSafetyError("generator diverges from authenticated frozen dataset")
    return dataset.database_rows()


async def prepare_pair(
    *,
    core_url: str,
    tracking_url: str,
    core_name: str,
    tracking_name: str,
    app_env: str,
    verify_only: bool = False,
) -> tuple[OwnerPreparation, OwnerPreparation]:
    """Report readiness only after both local commits and a fresh verification of both."""
    for url, name in ((core_url, core_name), (tracking_url, tracking_name)):
        _validate_invocation(
            "benchmark", database_url=url, app_env=app_env, confirmed_database_name=name
        )
    core, tracking = make_url(core_url), make_url(tracking_url)
    if (core.host, core.port) != (tracking.host, tracking.port):
        raise SeedSafetyError("both owners must use the same PostgreSQL instance")
    if core.database == tracking.database or core.username == tracking.username:
        raise SeedSafetyError("owners require distinct databases and credentials")
    rows = frozen_rows()
    results = []
    targets: tuple[tuple[Owner, str, str], ...] = (
        ("core", core_url, core_name),
        ("tracking", tracking_url, tracking_name),
    )
    for owner, url, name in targets:
        results.append(await prepare_owner(owner, url, name, rows, verify_only=verify_only))
    # A partial failure never produces a pair readiness report. Repetition may finish
    # an exact partially prepared pair, but never overwrites divergent contents.
    for owner, url, name in targets:
        await prepare_owner(owner, url, name, rows, verify_only=True)
    return results[0], results[1]


async def prepare_owner(
    owner: Owner,
    url: str,
    name: str,
    rows: dict[str, list[dict[str, object]]],
    *,
    verify_only: bool,
) -> OwnerPreparation:
    engine = create_async_engine(
        url, pool_size=1, max_overflow=0, connect_args={"options": "-c statement_timeout=30000"}
    )
    expected = {table: rows[table] for table in OWNER_TABLES[owner]}
    if owner == "tracking":
        expected["carrier_event_inbox"] = [
            dict(row, command=None) for row in rows["carrier_event_inbox"]
        ]
    inserted = False
    try:
        async with engine.begin() as connection:
            await _validate_postgresql_target(
                connection,
                confirmed_database_name=name,
                alembic_config_path=Path(f"alembic_{owner}.ini"),
            )
            schema = await owner_schema(connection, owner)
            metadata = await connection.run_sync(lambda conn: _reflect(conn, owner))
            if owner == "core":
                await _validate_official_carriers(connection, metadata.tables["carriers"])
                count = await connection.scalar(
                    sa.text("SELECT count(*) FROM tracking_event_receipts")
                )
                if count != 0:
                    raise SeedSafetyError("initial Core receipts must be empty")
            observed = await _read(connection, metadata, OWNER_TABLES[owner])
            if all(not values for values in observed.values()) and not verify_only:
                for table in OWNER_TABLES[owner]:
                    if expected[table]:
                        await connection.execute(sa.insert(metadata.tables[table]), expected[table])
                inserted = True
                observed = await _read(connection, metadata, OWNER_TABLES[owner])
            identity = compare_database_content(expected, observed)
        return OwnerPreparation(
            owner,
            name,
            schema,
            identity.global_sha256,
            {table: len(values) for table, values in expected.items()},
            inserted,
        )
    except SeedSafetyError:
        raise
    except Exception:
        raise SeedSafetyError(f"{owner} preparation failed; pair is not ready") from None
    finally:
        await engine.dispose()


def _reflect(connection: Connection, owner: Owner) -> sa.MetaData:
    metadata = sa.MetaData()
    metadata.reflect(bind=connection, only=OWNER_SCHEMA_TABLES[owner])
    return metadata


async def _read(
    connection: AsyncConnection, metadata: sa.MetaData, tables: tuple[str, ...]
) -> dict[str, list[dict[str, object]]]:
    result = {}
    for table in tables:
        result[table] = [
            dict(row)
            for row in (await connection.execute(sa.select(metadata.tables[table]))).mappings()
        ]
    return result


async def owner_schema(connection: AsyncConnection, owner: Owner) -> str:
    sections = {}
    for section, sql in STRUCTURAL_SCHEMA_QUERIES.items():
        sections[section] = [
            json.loads(row) for row in (await connection.execute(sa.text(sql))).scalars()
        ]
    return structural_schema_identity(
        sections, expected_table_names=OWNER_SCHEMA_TABLES[owner]
    ).sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-core-database", required=True)
    parser.add_argument("--confirm-tracking-database", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        result = run_async(
            prepare_pair(
                core_url=os.environ["CORE_DATABASE_URL"],
                tracking_url=os.environ["TRACKING_DATABASE_URL"],
                core_name=args.confirm_core_database,
                tracking_name=args.confirm_tracking_database,
                app_env=os.environ.get("APP_ENV", ""),
                verify_only=args.verify_only,
            )
        )
    except (SeedSafetyError, KeyError):
        print(
            "v1.1 preparation refused; both databases must be verified before use", file=sys.stderr
        )
        return 2
    print(
        json.dumps(
            {
                "ready": True,
                "dataset_sha256": FROZEN_DIGEST,
                "owners": [asdict(owner) for owner in result],
                "initial_receipts": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

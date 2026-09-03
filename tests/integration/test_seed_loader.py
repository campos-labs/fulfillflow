"""Atomic, idempotent, and fail-closed seeds on real PostgreSQL 18."""

from __future__ import annotations

import pytest
from benchmarks import seed_loader
from benchmarks.seed_loader import SeedSafetyError, load_dataset
from sqlalchemy import text

from fulfillflow.config import Settings
from fulfillflow.db import Database

pytestmark = pytest.mark.integration


async def test_demo_seed_is_atomic_and_exact_repetition_is_noop(
    postgres_database: Database,
    postgres_settings: Settings,
) -> None:
    database_name = _database_name(postgres_settings)

    first = await load_dataset(
        "demo",
        database_url=postgres_settings.database_dsn,
        app_env="test",
        confirmed_database_name=database_name,
    )
    second = await load_dataset(
        "demo",
        database_url=postgres_settings.database_dsn,
        app_env="test",
        confirmed_database_name=database_name,
    )

    assert first.inserted is True
    assert second.inserted is False
    assert first.logical_hash == second.logical_hash
    assert await _counts(postgres_database) == first.counts


async def test_benchmark_seed_persists_the_frozen_matrix(
    postgres_database: Database,
    postgres_settings: Settings,
) -> None:
    result = await load_dataset(
        "benchmark",
        database_url=postgres_settings.database_dsn,
        app_env="test",
        confirmed_database_name=_database_name(postgres_settings),
    )
    repeated = await load_dataset(
        "benchmark",
        database_url=postgres_settings.database_dsn,
        app_env="test",
        confirmed_database_name=_database_name(postgres_settings),
    )

    assert result.inserted is True
    assert repeated.inserted is False
    assert repeated.logical_hash == result.logical_hash
    assert result.counts["tracking_events"] == 15_000
    assert result.counts["notifications"] == 2_998
    assert await _counts(postgres_database) == result.counts
    async with postgres_database.engine.connect() as connection:
        mutable = await connection.scalar(
            text(
                "SELECT count(*) FROM shipments WHERE status IN ('IN_TRANSIT', 'OUT_FOR_DELIVERY')"
            )
        )
    assert mutable == 376


async def test_partially_populated_database_is_refused_without_writing(
    postgres_database: Database,
    postgres_settings: Settings,
) -> None:
    async with postgres_database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO orders "
                "(id, external_reference, recipient_name, recipient_email, "
                "recipient_postal_code, recipient_city, recipient_state, status, "
                "created_at, updated_at) VALUES "
                "('00000000-0000-4000-8000-000000000999', 'FOREIGN-ORDER', "
                "'Synthetic', 'foreign@example.test', '00000-000', 'Synthetic City', "
                "'SP', 'CREATED', now(), now())"
            )
        )

    with pytest.raises(SeedSafetyError, match="divergent or partially populated"):
        await load_dataset(
            "demo",
            database_url=postgres_settings.database_dsn,
            app_env="test",
            confirmed_database_name=_database_name(postgres_settings),
        )

    counts = await _counts(postgres_database)
    assert counts["orders"] == 1
    assert sum(counts.values()) == 1


async def test_same_count_single_row_mutation_is_refused(
    postgres_database: Database,
    postgres_settings: Settings,
) -> None:
    database_name = _database_name(postgres_settings)
    seeded = await load_dataset(
        "demo",
        database_url=postgres_settings.database_dsn,
        app_env="test",
        confirmed_database_name=database_name,
    )
    async with postgres_database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE orders SET recipient_city = 'Divergent Synthetic City' "
                "WHERE id = (SELECT id FROM orders ORDER BY id LIMIT 1)"
            )
        )

    with pytest.raises(SeedSafetyError, match="divergent or partially populated"):
        await load_dataset(
            "demo",
            database_url=postgres_settings.database_dsn,
            app_env="test",
            confirmed_database_name=database_name,
        )

    assert await _counts(postgres_database) == seeded.counts


async def test_all_counts_correct_but_notification_content_different_is_refused(
    postgres_database: Database,
    postgres_settings: Settings,
) -> None:
    database_name = _database_name(postgres_settings)
    seeded = await load_dataset(
        "demo",
        database_url=postgres_settings.database_dsn,
        app_env="test",
        confirmed_database_name=database_name,
    )
    async with postgres_database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE notifications SET message = 'Divergent synthetic message' "
                "WHERE id = (SELECT id FROM notifications ORDER BY id LIMIT 1)"
            )
        )

    with pytest.raises(SeedSafetyError, match="divergent or partially populated"):
        await load_dataset(
            "demo",
            database_url=postgres_settings.database_dsn,
            app_env="test",
            confirmed_database_name=database_name,
        )

    assert await _counts(postgres_database) == seeded.counts


@pytest.mark.parametrize("carrier_code", ["carrier-alpha", "carrier-beta"])
async def test_divergent_official_carrier_is_refused(
    carrier_code: str,
    postgres_database: Database,
    postgres_settings: Settings,
) -> None:
    database_name = _database_name(postgres_settings)
    async with postgres_database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE carriers SET name = 'Divergent Carrier' WHERE code = :code"),
            {"code": carrier_code},
        )
    try:
        with pytest.raises(SeedSafetyError, match="divergent reference data"):
            await load_dataset(
                "demo",
                database_url=postgres_settings.database_dsn,
                app_env="test",
                confirmed_database_name=database_name,
            )
        assert sum((await _counts(postgres_database)).values()) == 0
    finally:
        official_name = "Carrier Alpha" if carrier_code == "carrier-alpha" else "Carrier Beta"
        async with postgres_database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE carriers SET name = :name WHERE code = :code"),
                {"name": official_name, "code": carrier_code},
            )


async def test_unexpected_insert_failure_rolls_back_every_table(
    monkeypatch: pytest.MonkeyPatch,
    postgres_database: Database,
    postgres_settings: Settings,
) -> None:
    original = seed_loader._insert_dataset

    async def insert_then_fail(*args: object, **kwargs: object) -> None:
        await original(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("synthetic failure after all inserts")

    monkeypatch.setattr(seed_loader, "_insert_dataset", insert_then_fail)

    with pytest.raises(SeedSafetyError, match="transaction was not committed"):
        await load_dataset(
            "demo",
            database_url=postgres_settings.database_dsn,
            app_env="test",
            confirmed_database_name=_database_name(postgres_settings),
        )

    assert await _counts(postgres_database) == {
        "orders": 0,
        "shipments": 0,
        "carrier_event_inbox": 0,
        "tracking_events": 0,
        "notifications": 0,
    }


def _database_name(settings: Settings) -> str:
    return settings.database_dsn.rsplit("/", maxsplit=1)[-1]


async def _counts(database: Database) -> dict[str, int]:
    tables = (
        "orders",
        "shipments",
        "carrier_event_inbox",
        "tracking_events",
        "notifications",
    )
    async with database.engine.connect() as connection:
        return {
            table: int(await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for table in tables
        }

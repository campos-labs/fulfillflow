"""Both owner databases must contain the exact frozen projection before readiness."""

import pytest
from benchmarks import seed_v11
from benchmarks.seed_loader import SeedSafetyError
from sqlalchemy import text

from fulfillflow.config import Settings
from fulfillflow.db import Database

pytestmark = pytest.mark.integration


async def test_pair_seed_repeat_and_partial_failure_recovery(
    postgres_database: Database,
    postgres_tracking_database: Database,
    postgres_settings: Settings,
    postgres_tracking_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = dict(
        core_url=postgres_settings.database_dsn,
        tracking_url=postgres_tracking_settings.database_dsn,
        core_name="fulfillflow_core",
        tracking_name="fulfillflow_tracking",
        app_env="test",
    )
    original = seed_v11.prepare_owner

    async def fail_tracking(owner, *positional, **kwargs):
        if owner == "tracking":
            raise SeedSafetyError("synthetic Tracking unavailable after Core preparation")
        return await original(owner, *positional, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(seed_v11, "prepare_owner", fail_tracking)
        with pytest.raises(SeedSafetyError, match="Tracking unavailable"):
            await seed_v11.prepare_pair(**args)
    async with postgres_database.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM shipments")) == 1500
    async with postgres_tracking_database.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM tracking_events")) == 0
    recovered = await seed_v11.prepare_pair(**args)
    assert [owner.inserted for owner in recovered] == [False, True]
    repeated = await seed_v11.prepare_pair(**args, verify_only=True)
    assert [owner.inserted for owner in repeated] == [False, False]
    assert [owner.content_sha256 for owner in repeated] == [
        owner.content_sha256 for owner in recovered
    ]
    assert repeated[0].schema_sha256 != repeated[1].schema_sha256
    assert repeated[1].counts == {"carrier_event_inbox": 15000, "tracking_events": 15000}
    async with postgres_tracking_database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE carrier_event_inbox SET command = '{}' "
                "WHERE id = (SELECT id FROM carrier_event_inbox LIMIT 1)"
            )
        )
    with pytest.raises(SeedSafetyError, match="not ready"):
        await seed_v11.prepare_pair(**args)


async def test_pair_refuses_same_owner_before_writing(postgres_settings: Settings) -> None:
    with pytest.raises(SeedSafetyError, match="distinct"):
        await seed_v11.prepare_pair(
            core_url=postgres_settings.database_dsn,
            tracking_url=postgres_settings.database_dsn,
            core_name="fulfillflow_core",
            tracking_name="fulfillflow_core",
            app_env="test",
        )

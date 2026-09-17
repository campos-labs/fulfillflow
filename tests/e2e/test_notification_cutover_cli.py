"""Offline frozen-schema copy, owner CLIs and authenticated reads preserve legacy history."""

import asyncio
import hashlib
import json
import os

import aio_pika
import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text

from fulfillflow.messaging.amqp import declare_flow, publish
from tests.e2e.test_external_simulator_journey import (
    _available_port,
    _start_application,
    _stop_application,
    _wait_until_ready,
)
from tests.e2e.test_notification_recovery import eventually, scalar, start
from tests.integration.test_notification_cutover import seed_core_archive
from tests.notification_support import notification_message
from tests.operations_support import command, owner_environment
from tests.service_pair import create_app

pytestmark = pytest.mark.integration


async def test_offline_cli_cutover_and_legacy_replay_remain_queryable_without_resimulation(
    postgres_database,
    postgres_settings,
    postgres_tracking_database,
    postgres_notifications_database,
    postgres_notifications_settings,
    fixed_clock,
    tmp_path,
    monkeypatch,
):
    assert (
        await scalar(
            postgres_tracking_database, "SELECT count(*) FROM message_inbox WHERE state <> 'DONE'"
        )
        == 0
    )
    assert (
        await scalar(
            postgres_tracking_database, "SELECT count(*) FROM message_outbox WHERE state <> 'SENT'"
        )
        == 0
    )
    # All writers/admission are absent in this disposable copy. Never mutate historical volumes.
    monkeypatch.setenv("DATABASE_URL", postgres_settings.database_dsn)
    configuration = Config("alembic_core.ini")
    await asyncio.to_thread(alembic_command.downgrade, configuration, "1202_core")
    children = []
    try:
        original = await seed_core_archive(postgres_database)
        async with postgres_database.session() as session, session.begin():
            await session.execute(
                text("""INSERT INTO tracking_event_receipts
                (event_id,carrier_id,external_event_id,content_sha256,result,created_at)
                VALUES (:id,'00000000-0000-4000-8000-000000000100','offline-original',:hash,
                CAST(:result AS jsonb),:now)"""),
                dict(
                    id=original.tracking_event_id,
                    hash="a" * 64,
                    now=fixed_clock.now(),
                    result=json.dumps(
                        dict(
                            kind="applied",
                            event_id=str(original.tracking_event_id),
                            shipment_id=str(original.shipment_id),
                            result="APPLIED",
                            previous_status="PENDING",
                            current_status="POSTED",
                            decided_at=original.created_at.isoformat(),
                        )
                    ),
                ),
            )
        archive = tmp_path / "legacy.json"
        exported = await command(
            postgres_settings,
            "fulfillflow.core.notification_legacy",
            "--output",
            archive,
            "--expected-database",
            postgres_database.engine.url.database,
        )
        assert exported.returncode == 0, exported.stdout
        snapshot_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        content = json.loads(archive.read_text())
        assert content["applied_event_ids"] == [str(original.tracking_event_id)]
        for _ in range(2):
            imported = await command(
                postgres_notifications_settings,
                "fulfillflow.notifications.cutover",
                "--input",
                archive,
                "--expected-database",
                postgres_notifications_database.engine.url.database,
            )
            assert imported.returncode == 0, imported.stdout
        await asyncio.to_thread(alembic_command.upgrade, configuration, "head")
        port = _available_port()
        url = f"http://127.0.0.1:{port}"
        environment = owner_environment(postgres_notifications_settings)
        environment.update(APP_HOST="127.0.0.1", APP_PORT=str(port))
        api = _start_application(environment)
        children.append(api)
        await _wait_until_ready(api, url)
        settings = postgres_settings.model_copy(update={"notifications_base_url": url})
        app = create_app(settings, postgres_database, clock=fixed_clock)
        app.state.notifications_transport = None
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
        ):
            before = (await client.get(f"/api/v1/notifications/{original.id}")).json()
            assert before == original.model_dump(mode="json")
            progress = await client.get(f"/api/v1/notification-status/{original.tracking_event_id}")
            assert progress.status_code == 200 and progress.json()["origin"] == "LEGACY"
            assert progress.json()["publication"] is None and progress.json()["processing"] is None
            worker = start(postgres_notifications_settings, tmp_path / "notifications.json")
            children.append(worker)
            connection = await aio_pika.connect(os.environ["TEST_AMQP_URL"], timeout=5)
            async with connection:
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                message = notification_message()
                await declare_flow(channel, message.type)
                await publish(channel, message)

                async def suppressed():
                    return (
                        await scalar(
                            postgres_notifications_database, "SELECT state FROM message_inbox"
                        )
                        == "DONE"
                    )

                await eventually(suppressed)
                await publish(channel, message)
            report = await command(
                postgres_notifications_settings,
                "fulfillflow.notifications.operations",
                "diagnose",
                "--id",
                message.event_id,
            )
            assert report.returncode == 0
            diagnostic = json.loads(report.stdout)
            assert diagnostic["business"]["terminals"][0]["origin"] == "LEGACY"
            assert diagnostic["business"]["terminals"][0]["message_id"] is None
            assert diagnostic["quarantine_count"] == 1
            assert diagnostic["quarantine_recent"][0]["reason"] == "LEGACY_EVENT_SUPPRESSED"
            assert original.recipient not in report.stdout
            assert (await client.get(f"/api/v1/notifications/{original.id}")).json() == before
        assert await scalar(postgres_database, "SELECT count(*) FROM message_outbox") == 0
        assert await scalar(postgres_database, "SELECT count(*) FROM notifications") == 1
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == snapshot_hash
    finally:
        for child in reversed(children):
            await asyncio.to_thread(_stop_application, child)
        await asyncio.to_thread(alembic_command.upgrade, configuration, "head")

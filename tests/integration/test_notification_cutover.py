"""Offline cutover proves ownership, drain, receipt correspondence and exact copying."""

import asyncio
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from tests.integration.test_notifications_owned import import_records, legacy_record
from tests.message_support import NOW, command_message
from tests.support import FixedClock

from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.core.notification_legacy import LegacyExportError, export_legacy
from fulfillflow.messaging.store import put_message
from fulfillflow.notifications.owned_service import OwnedNotificationService

pytestmark = pytest.mark.integration


async def seed_core_archive(database):
    record = legacy_record()
    async with database.session() as session, session.begin():
        await session.execute(
            text("""
            INSERT INTO orders (id,external_reference,recipient_name,recipient_email,
                recipient_postal_code,recipient_city,recipient_state,status,created_at,updated_at)
            VALUES (:id,'CUTOVER-ORDER','Synthetic Recipient','legacy@example.test',
                '00000-000','Synthetic City','SP','CONFIRMED',:now,:now)
        """),
            {"id": UUID(int=9001), "now": NOW},
        )
        await session.execute(
            text("""
            INSERT INTO shipments (id,order_id,carrier_id,tracking_code,status,
                status_occurred_at,created_at,updated_at)
            VALUES (:id,:order,'00000000-0000-4000-8000-000000000100','CUTOVER-SHIPMENT',
                'POSTED',:now,:now,:now)
        """),
            {"id": record.shipment_id, "order": UUID(int=9001), "now": NOW},
        )
        await session.execute(
            text("""
            INSERT INTO notifications (id,shipment_id,tracking_event_id,channel,recipient,
                template_key,message,status,error_detail,created_at,simulated_at)
            VALUES (:id,:shipment_id,:tracking_event_id,:channel,:recipient,:template_key,
                :message,:status,:error_detail,:created_at,:simulated_at)
        """),
            record.model_dump(),
        )
    return record


async def export(database, expected_database=None):
    async with database.session() as session:
        return await export_legacy(
            session, expected_database=expected_database or database.engine.url.database
        )


async def test_cutover_preserves_full_record_without_inventing_receipt_or_envelope(
    postgres_database, postgres_notifications_database, monkeypatch
):
    # This fixture database is disposable. Reconstruct the frozen Core schema,
    # export its actual records, and upgrade the source without changing history.
    monkeypatch.setenv(
        "DATABASE_URL", postgres_database.engine.url.render_as_string(hide_password=False)
    )
    config = Config("alembic_core.ini")
    await asyncio.to_thread(command.downgrade, config, "1202_core")
    try:
        async with postgres_database.session() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version")) == "1202_core"
            )
        original = await seed_core_archive(postgres_database)
        archive = await export(postgres_database)
        assert archive.notifications == [original]
        assert archive.applied_event_ids == []
        await import_records(postgres_notifications_database, *archive.notifications)
        async with postgres_notifications_database.session() as session:
            service = OwnedNotificationService(session, FixedClock(NOW))
            assert await service.get(original.id) == original
            progress = await service.progress(original.tracking_event_id)
            assert progress.origin == "LEGACY" and progress.processing is None
            assert await session.scalar(text("SELECT count(*) FROM message_inbox")) == 0
        assert (await export(postgres_database)).notifications == [original]
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    assert (await export(postgres_database)).notifications == [original]


async def test_export_refuses_wrong_owner_undrained_core_and_missing_applied_record(
    postgres_database,
):
    database = postgres_database
    await seed_core_archive(database)
    with pytest.raises(LegacyExportError, match="WRONG_CORE_DATABASE"):
        await export(database, "notifications-is-not-core")
    async with database.session() as session, session.begin():
        await put_message(session, core_tables.inbox, command_message(), NOW)
    with pytest.raises(LegacyExportError, match="CORE_WORK_NOT_DRAINED"):
        await export(database)
    async with database.session() as session, session.begin():
        await session.execute(text("UPDATE message_inbox SET state='BLOCKED'"))
    with pytest.raises(LegacyExportError, match="CORE_WORK_NOT_DRAINED"):
        await export(database)
    async with database.session() as session, session.begin():
        await session.execute(text("UPDATE message_inbox SET state='DONE'"))
        await session.execute(
            text("""
            INSERT INTO tracking_event_receipts(event_id,carrier_id,external_event_id,
                content_sha256,result,created_at)
            VALUES (:id,'00000000-0000-4000-8000-000000000100','cutover-applied',:hash,
                '{"kind":"applied","result":"APPLIED"}',:now)
        """),
            {"id": UUID(int=2), "hash": "a" * 64, "now": NOW},
        )
    with pytest.raises(LegacyExportError, match="LEGACY_RECEIPT_OR_CONTENT_CONFLICT"):
        await export(database)
    async with database.session() as session, session.begin():
        await session.execute(
            text("UPDATE tracking_event_receipts SET event_id=:id"), {"id": UUID(int=1)}
        )
    assert (await export(database)).applied_event_ids == [UUID(int=1)]

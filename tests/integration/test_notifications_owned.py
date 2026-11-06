"""Notifications records, inbox disposition and cutover use real PostgreSQL."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from tests.message_support import NOW
from tests.notification_support import notification_message
from tests.support import FixedClock

from fulfillflow.contracts.notifications import LegacyNotificationArchive, NotificationRead
from fulfillflow.messaging.store import process_one, put_message
from fulfillflow.notifications.cutover import LegacyImportError, import_legacy
from fulfillflow.notifications.message_handler import apply_notification
from fulfillflow.notifications.message_tables import tables
from fulfillflow.notifications.owned_models import OwnedNotificationModel
from fulfillflow.notifications.owned_service import OwnedNotificationService

pytestmark = pytest.mark.integration


def legacy_record(number=1):
    return NotificationRead(
        id=UUID(int=number + 8000),
        shipment_id=UUID(int=number + 5000),
        tracking_event_id=UUID(int=number),
        channel="EMAIL",
        recipient="legacy@example.test",
        template_key="shipment_posted",
        message="Your shipment has been posted.",
        status="SIMULATED",
        error_detail=None,
        created_at=NOW - timedelta(days=1),
        simulated_at=NOW - timedelta(days=1),
    )


async def import_records(database, *records):
    async with database.session() as session:
        return await import_legacy(
            session,
            LegacyNotificationArchive(notifications=list(records), applied_event_ids=[]),
            expected_database=make_url(str(database.engine.url)).database,
        )


async def admit(database, number=1):
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, notification_message(number), NOW)


async def process(database):
    async with database.session() as session, session.begin():
        return await process_one(
            session,
            tables.inbox,
            NOW,
            lambda session, message: apply_notification(session, message, FixedClock(NOW)),
        )


async def test_concurrent_processors_commit_one_record_and_done(postgres_notifications_database):
    database = postgres_notifications_database
    await admit(database)
    results = await asyncio.gather(process(database), process(database))
    assert sorted(results) == [False, True]
    async with database.session() as session:
        row = await session.scalar(select(OwnedNotificationModel))
        assert row.tracking_event_id == UUID(int=1)
        original = NotificationRead.model_validate(row)
        assert row.origin == "ASYNC"
        assert await session.scalar(select(tables.inbox.c.state)) == "DONE"
    await admit(database)
    assert not await process(database)
    async with database.session() as session:
        service = OwnedNotificationService(session, FixedClock(NOW))
        assert await service.get(original.id) == original
        progress = await service.progress(UUID(int=1))
        assert progress.processing == "DONE" and progress.notification_id == original.id
        counts = await service.counts()
        assert (counts.simulated, counts.failed) == (1, 0)


async def test_record_and_done_rollback_together(postgres_notifications_database):
    database = postgres_notifications_database
    await admit(database)
    with pytest.raises(RuntimeError, match="after done"):
        async with database.session() as session, session.begin():
            assert await process_one(
                session,
                tables.inbox,
                NOW,
                lambda session, message: apply_notification(session, message, FixedClock(NOW)),
            )
            raise RuntimeError("after done")
    async with database.session() as session:
        assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
        assert await session.scalar(select(tables.inbox.c.state)) == "PENDING"
    assert await process(database)


async def test_legacy_suppression_preserves_all_fields_and_is_atomic(
    postgres_notifications_database,
):
    database = postgres_notifications_database
    original = legacy_record()
    await import_records(database, original)
    await admit(database)
    with pytest.raises(RuntimeError, match="after quarantine"):
        async with database.session() as session, session.begin():
            await process_one(
                session,
                tables.inbox,
                NOW,
                lambda session, message: apply_notification(session, message, FixedClock(NOW)),
            )
            raise RuntimeError("after quarantine")
    async with database.session() as session:
        assert await session.scalar(text("SELECT count(*) FROM message_quarantine")) == 0
        assert await session.scalar(select(tables.inbox.c.state)) == "PENDING"
    assert await process(database)
    await admit(database)
    assert not await process(database)
    async with database.session() as session:
        assert await session.scalar(select(tables.quarantine.c.reason)) == "LEGACY_EVENT_SUPPRESSED"
        assert await session.scalar(text("SELECT count(*) FROM message_quarantine")) == 1
        assert await session.scalar(select(tables.inbox.c.state)) == "DONE"
        service = OwnedNotificationService(session, FixedClock(NOW))
        await session.rollback()
        assert await service.get(original.id) == original
        progress = await service.progress(original.tracking_event_id)
        assert progress.origin == "LEGACY" and progress.processing is None


async def test_import_retry_conflict_and_target_activity(postgres_notifications_database):
    database = postgres_notifications_database
    original = legacy_record()
    assert await import_records(database, original) == 1
    assert await import_records(database, original) == 1
    changed = original.model_copy(update={"recipient": "changed@example.test"})
    with pytest.raises(LegacyImportError, match="CONTENT_CONFLICT"):
        await import_records(database, legacy_record(2), changed)
    async with database.session() as session:
        assert await session.scalar(text("SELECT count(*) FROM notifications")) == 1
        assert (
            NotificationRead.model_validate(await session.get(OwnedNotificationModel, original.id))
            == original
        )
    with pytest.raises(LegacyImportError, match="COUNT_CONFLICT"):
        await import_records(database)
    await admit(database, 2)
    with pytest.raises(LegacyImportError, match="ALREADY_RECEIVING"):
        await import_records(database, original)


async def test_progress_distinguishes_unknown_pending_blocked_and_legacy(
    postgres_notifications_database,
):
    database = postgres_notifications_database
    await import_records(database, legacy_record(2))
    async with database.session() as session:
        service = OwnedNotificationService(session, FixedClock(NOW))
        assert (await service.progress(UUID(int=1))).processing == "NOT_RECEIVED"
        assert (await service.progress(UUID(int=2))).origin == "LEGACY"
    await admit(database)
    async with database.session() as session:
        assert (
            await OwnedNotificationService(session, FixedClock(NOW)).progress(UUID(int=1))
        ).processing == "PENDING"
    async with database.session() as session, session.begin():
        await session.execute(update(tables.inbox).values(state="BLOCKED", reason="test"))
    async with database.session() as session:
        assert (
            await OwnedNotificationService(session, FixedClock(NOW)).progress(UUID(int=1))
        ).processing == "BLOCKED"


async def test_reverse_arrival_keeps_each_fact_and_preserves_query_filters(
    postgres_notifications_database,
):
    database = postgres_notifications_database
    for number in (2, 1):
        await admit(database, number)
        assert await process(database)
    async with database.session() as session:
        service = OwnedNotificationService(session, FixedClock(NOW))
        complete = await service.list(created_from=NOW, created_to=NOW)
        assert complete.total == 2
        assert [item.id for item in complete.items] == sorted(
            [item.id for item in complete.items], reverse=True
        )
        first = await service.list(page_size=1)
        second = await service.list(page=2, page_size=1)
        assert first.items + second.items == complete.items
        filtered = await service.list(
            shipment_id=notification_message().payload.shipment_id, status="SIMULATED"
        )
        assert filtered.total == 1 and filtered.items[0].tracking_event_id == UUID(int=1)


async def test_simulation_failure_terminal_does_not_become_technical_failure(
    postgres_notifications_database, monkeypatch
):
    from fulfillflow.notifications import domain

    database = postgres_notifications_database
    monkeypatch.delitem(domain._STATUS_CONTENT, "POSTED")
    await admit(database)
    assert await process(database)
    async with database.session() as session:
        progress = await OwnedNotificationService(session, FixedClock(NOW)).progress(UUID(int=1))
        assert progress.processing == "DONE" and progress.status == "FAILED"
        assert progress.simulated_at is None


async def test_model_has_no_foreign_keys_to_other_services(postgres_notifications_database):
    async with postgres_notifications_database.session() as session:
        targets = (
            (
                await session.execute(
                    text("""
            SELECT confrelid::regclass::text FROM pg_constraint
            WHERE conrelid='notifications'::regclass AND contype='f'
        """)
                )
            )
            .scalars()
            .all()
        )
        assert targets == ["message_inbox"]
        assert await session.scalar(text("SELECT to_regclass('message_outbox')")) is None


async def test_database_unique_effect_is_final_concurrency_authority(
    postgres_notifications_database,
):
    database = postgres_notifications_database
    first = legacy_record()
    second = first.model_copy(update={"id": UUID(int=9999)})

    async def write(record):
        try:
            async with database.session() as session, session.begin():
                session.add(
                    OwnedNotificationModel(**record.model_dump(), origin="LEGACY", message_id=None)
                )
                await session.flush()
        except IntegrityError as error:
            return error.orig.diag.constraint_name
        return "inserted"

    results = await asyncio.gather(write(first), write(second))
    assert sorted(results) == ["inserted", "uq_notifications_tracking_event_id"]
    async with database.session() as session:
        assert await session.scalar(text("SELECT count(*) FROM notifications")) == 1


async def test_import_checks_owner_database_and_head(postgres_notifications_database):
    database = postgres_notifications_database
    async with database.session() as session:
        with pytest.raises(LegacyImportError, match="WRONG_NOTIFICATIONS_DATABASE_OR_HEAD"):
            await import_legacy(
                session,
                LegacyNotificationArchive(notifications=[], applied_event_ids=[]),
                expected_database="wrong-owner",
            )

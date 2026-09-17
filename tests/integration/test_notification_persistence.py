"""Real PostgreSQL checks for Notifications-owned persistence and queries."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.notification_support import notification_message

from fulfillflow.contracts.notifications import NotificationRead
from fulfillflow.db import Database
from fulfillflow.messaging.store import put_message
from fulfillflow.notifications.domain import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from fulfillflow.notifications.message_tables import tables
from fulfillflow.notifications.owned_repository import (
    OwnedNotificationRepository as NotificationRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
CARRIER_ID = UUID("00000000-0000-4000-8000-000000000920")
ORDER_ID = UUID("00000000-0000-4000-8000-000000000921")
SHIPMENT_ID = UUID("00000000-0000-4000-8000-000000000922")
SECOND_SHIPMENT_ID = UUID("00000000-0000-4000-8000-000000000923")
INBOX_IDS = tuple(UUID(int=value) for value in range(924, 928))
TRACKING_EVENT_IDS = tuple(UUID(int=value) for value in range(928, 932))
REQUEST_IDS = tuple(UUID(int=value) for value in range(932, 936))
NOTIFICATION_IDS = tuple(UUID(int=value) for value in range(936, 940))


async def _add(session, notification):
    # Separate technical identities let this low-level test target business uniqueness.
    message = notification_message(notification.id.int)
    await put_message(session, tables.inbox, message, NOW)
    await NotificationRepository(session).add(notification, message.message_id)
    return message.message_id


def _notification(
    index: int,
    *,
    status: NotificationStatus,
    created_at: datetime,
    shipment_id: UUID | None = None,
    tracking_event_id: UUID | None = None,
) -> Notification:
    simulated = status is NotificationStatus.SIMULATED
    return Notification(
        id=NOTIFICATION_IDS[index],
        shipment_id=shipment_id or (SHIPMENT_ID if index < 2 else SECOND_SHIPMENT_ID),
        tracking_event_id=tracking_event_id or TRACKING_EVENT_IDS[index],
        channel=NotificationChannel.EMAIL,
        recipient="notification@example.test",
        template_key=("shipment_in_transit" if simulated else "shipment_notification_failed"),
        message=(
            "Your shipment is in transit."
            if simulated
            else "The shipment notification could not be simulated."
        ),
        status=status,
        error_detail=None if simulated else "Notification rendering or simulation failed.",
        created_at=created_at,
        simulated_at=created_at if simulated else None,
    )


async def test_repository_roundtrip_filters_stable_order_and_pagination(
    postgres_notifications_database: Database,
) -> None:
    notifications = (
        _notification(0, status=NotificationStatus.SIMULATED, created_at=NOW),
        _notification(
            1,
            status=NotificationStatus.FAILED,
            created_at=NOW + timedelta(minutes=1),
        ),
        _notification(
            2,
            status=NotificationStatus.SIMULATED,
            created_at=NOW + timedelta(minutes=1),
        ),
        _notification(
            3,
            status=NotificationStatus.FAILED,
            created_at=NOW + timedelta(minutes=2),
        ),
    )

    async with postgres_notifications_database.session() as session, session.begin():
        repository = NotificationRepository(session)
        for notification in notifications:
            await _add(session, notification)

    async with postgres_notifications_database.session() as session:
        repository = NotificationRepository(session)
        simulated = await repository.get(NOTIFICATION_IDS[0])
        failed = await repository.get(NOTIFICATION_IDS[1])
        missing = await repository.get(UUID(int=99_999))
        first_page = await repository.list(
            status=None,
            shipment_id=None,
            created_from=None,
            created_to=None,
            page=1,
            page_size=2,
        )
        second_page = await repository.list(
            status=None,
            shipment_id=None,
            created_from=None,
            created_to=None,
            page=2,
            page_size=2,
        )
        failed_page = await repository.list(
            status=NotificationStatus.FAILED,
            shipment_id=None,
            created_from=None,
            created_to=None,
            page=1,
            page_size=25,
        )
        shipment_page = await repository.list(
            status=None,
            shipment_id=SHIPMENT_ID,
            created_from=None,
            created_to=None,
            page=1,
            page_size=25,
        )
        bounded_page = await repository.list(
            status=None,
            shipment_id=None,
            created_from=NOW + timedelta(minutes=1),
            created_to=NOW + timedelta(minutes=1),
            page=1,
            page_size=25,
        )
        physical_types = (
            await session.execute(
                text(
                    "SELECT pg_typeof(id)::text, pg_typeof(created_at)::text, "
                    "pg_typeof(simulated_at)::text FROM notifications WHERE id = :id"
                ),
                {"id": NOTIFICATION_IDS[0]},
            )
        ).one()

    assert simulated is not None
    simulated = simulated.record
    assert simulated == NotificationRead.model_validate(notifications[0])
    assert failed is not None
    failed = failed.record
    assert failed == NotificationRead.model_validate(notifications[1])
    assert missing is None
    assert simulated is not None
    assert failed is not None
    assert isinstance(simulated.id, UUID)
    assert simulated.channel == "EMAIL"
    assert simulated.status == "SIMULATED"
    assert simulated.created_at.utcoffset() == timedelta(0)
    assert simulated.simulated_at is not None
    assert simulated.simulated_at.utcoffset() == timedelta(0)
    assert failed.status == "FAILED"
    assert failed.simulated_at is None
    assert failed.error_detail == "Notification rendering or simulation failed."

    notifications = tuple(NotificationRead.model_validate(item) for item in notifications)
    assert first_page.items == [notifications[3], notifications[2]]
    assert (first_page.page, first_page.page_size, first_page.total) == (1, 2, 4)
    assert second_page.items == [notifications[1], notifications[0]]
    assert (second_page.page, second_page.page_size, second_page.total) == (2, 2, 4)
    assert failed_page.items == [notifications[3], notifications[1]]
    assert failed_page.total == 2
    assert shipment_page.items == [notifications[1], notifications[0]]
    assert shipment_page.total == 2
    assert bounded_page.items == [notifications[2], notifications[1]]
    assert bounded_page.total == 2
    assert tuple(physical_types) == (
        "uuid",
        "timestamp with time zone",
        "timestamp with time zone",
    )


async def test_tracking_event_unique_constraint_is_the_idempotency_authority(
    postgres_notifications_database: Database,
) -> None:
    original = _notification(
        0,
        status=NotificationStatus.SIMULATED,
        created_at=NOW,
    )
    duplicate = _notification(
        1,
        status=NotificationStatus.SIMULATED,
        created_at=NOW + timedelta(seconds=1),
        tracking_event_id=TRACKING_EVENT_IDS[0],
    )

    async with postgres_notifications_database.session() as session, session.begin():
        await _add(session, original)

    with pytest.raises(IntegrityError) as error:
        async with postgres_notifications_database.session() as session, session.begin():
            await _add(session, duplicate)

    diagnostic = getattr(error.value.orig, "diag", None)
    assert diagnostic is not None
    assert diagnostic.constraint_name == "uq_notifications_tracking_event_id"
    async with postgres_notifications_database.session() as session:
        count = await session.scalar(text("SELECT count(*) FROM notifications"))
        persisted = await NotificationRepository(session).get(original.id)
    assert count == 1
    assert persisted.record == NotificationRead.model_validate(original)


async def test_external_references_have_no_fk_and_local_causal_history_is_restricted(
    postgres_notifications_database: Database,
) -> None:
    database = postgres_notifications_database
    original = _notification(
        0,
        status=NotificationStatus.SIMULATED,
        created_at=NOW,
        shipment_id=UUID(int=99991),
        tracking_event_id=UUID(int=99992),
    )
    async with database.session() as session, session.begin():
        message_id = await _add(session, original)
    with pytest.raises(IntegrityError) as error:
        async with database.session() as session, session.begin():
            await session.execute(
                text("DELETE FROM message_inbox WHERE message_id=:id"), {"id": message_id}
            )
    assert error.value.orig.diag.constraint_name == "fk_notifications_message_id_message_inbox"
    async with database.session() as session:
        stored = await NotificationRepository(session).get(original.id)
        assert stored.record == NotificationRead.model_validate(original)
        foreign_keys = (
            (
                await session.execute(
                    text(
                        "SELECT conname,confdeltype::text FROM pg_constraint "
                        "WHERE conrelid='notifications'::regclass AND contype='f'"
                    )
                )
            )
            .tuples()
            .all()
        )
        assert foreign_keys == [("fk_notifications_message_id_message_inbox", "r")]


async def test_database_enforces_notification_enums_nonempty_content_and_index(
    postgres_notifications_database: Database,
) -> None:
    notification = _notification(
        0,
        status=NotificationStatus.SIMULATED,
        created_at=NOW,
    )
    async with postgres_notifications_database.session() as session, session.begin():
        await _add(session, notification)

    invalid_updates = (
        ("channel", "SMS", "ck_notifications_notification_channel"),
        ("status", "PENDING", "ck_notifications_notification_status"),
        ("recipient", "   ", "ck_notifications_recipient_nonempty"),
        ("template_key", "   ", "ck_notifications_template_key_nonempty"),
        ("message", "   ", "ck_notifications_message_nonempty"),
    )
    for field, value, constraint_name in invalid_updates:
        with pytest.raises(IntegrityError) as error:
            async with postgres_notifications_database.session() as session, session.begin():
                await session.execute(
                    text(f"UPDATE notifications SET {field} = :value WHERE id = :id"),
                    {"id": notification.id, "value": value},
                )
        diagnostic = getattr(error.value.orig, "diag", None)
        assert diagnostic is not None
        assert diagnostic.constraint_name == constraint_name

    async with postgres_notifications_database.session() as session:
        persisted = await NotificationRepository(session).get(notification.id)
        index_definition = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'ix_notifications_status_created_at'"
            )
        )
    assert persisted.record == NotificationRead.model_validate(notification)
    assert index_definition is not None
    assert "(status, created_at DESC)" in index_definition

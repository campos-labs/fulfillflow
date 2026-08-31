"""Real PostgreSQL checks for Notifications-owned persistence and queries."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fulfillflow.db import Database
from fulfillflow.notifications.domain import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from fulfillflow.notifications.repository import NotificationRepository

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


async def _insert_owners_and_events(database: Database, *, event_count: int = 4) -> None:
    shipment_ids = (SHIPMENT_ID, SHIPMENT_ID, SECOND_SHIPMENT_ID, SECOND_SHIPMENT_ID)
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO carriers "
                "(id, code, name, adapter_key, active, created_at, updated_at) "
                "VALUES (:id, 'notification-persistence', 'Notification Persistence', "
                "'notification-persistence', true, :now, :now)"
            ),
            {"id": CARRIER_ID, "now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO orders "
                "(id, external_reference, recipient_name, recipient_email, "
                "recipient_postal_code, recipient_city, recipient_state, status, "
                "created_at, updated_at) VALUES "
                "(:id, 'NOTIFICATION-PERSISTENCE', 'Notification Recipient', "
                "'notification@example.test', '09700-000', 'Sao Bernardo do Campo', "
                "'SP', 'CONFIRMED', :now, :now)"
            ),
            {"id": ORDER_ID, "now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO shipments "
                "(id, order_id, carrier_id, tracking_code, status, status_occurred_at, "
                "status_event_received_at, status_external_event_id, "
                "estimated_delivery_date, shipped_at, delivered_at, created_at, updated_at) "
                "VALUES "
                "(:first_id, :order_id, :carrier_id, 'NOTIFICATION-ONE', 'POSTED', "
                ":now, NULL, NULL, NULL, :now, NULL, :now, :now), "
                "(:second_id, :order_id, :carrier_id, 'NOTIFICATION-TWO', 'POSTED', "
                ":now, NULL, NULL, NULL, :now, NULL, :now, :now)"
            ),
            {
                "first_id": SHIPMENT_ID,
                "second_id": SECOND_SHIPMENT_ID,
                "order_id": ORDER_ID,
                "carrier_id": CARRIER_ID,
                "now": NOW,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO carrier_event_inbox "
                "(id, carrier_id, external_event_id, payload_sha256, raw_body, "
                "parsed_payload, received_at, status, error_code, error_detail, "
                "processed_at, request_id) VALUES "
                "(:id, :carrier_id, :external_event_id, :payload_sha256, :raw_body, "
                "CAST(:parsed_payload AS jsonb), :received_at, 'PROCESSED', NULL, NULL, "
                ":processed_at, :request_id)"
            ),
            [
                {
                    "id": INBOX_IDS[index],
                    "carrier_id": CARRIER_ID,
                    "external_event_id": f"notification-event-{index}",
                    "payload_sha256": f"{index + 1:x}" * 64,
                    "raw_body": b"{}",
                    "parsed_payload": "{}",
                    "received_at": NOW + timedelta(minutes=index),
                    "processed_at": NOW + timedelta(minutes=index),
                    "request_id": REQUEST_IDS[index],
                }
                for index in range(event_count)
            ],
        )
        await connection.execute(
            text(
                "INSERT INTO tracking_events "
                "(id, inbox_event_id, shipment_id, carrier_id, external_status, "
                "canonical_status, description, location, occurred_at, received_at, "
                "application_result, previous_shipment_status, resulting_shipment_status, "
                "created_at) VALUES "
                "(:id, :inbox_event_id, :shipment_id, :carrier_id, 'in_transit', "
                "'IN_TRANSIT', NULL, NULL, :occurred_at, :received_at, 'APPLIED', "
                "'POSTED', 'IN_TRANSIT', :created_at)"
            ),
            [
                {
                    "id": TRACKING_EVENT_IDS[index],
                    "inbox_event_id": INBOX_IDS[index],
                    "shipment_id": shipment_ids[index],
                    "carrier_id": CARRIER_ID,
                    "occurred_at": NOW + timedelta(minutes=index),
                    "received_at": NOW + timedelta(minutes=index),
                    "created_at": NOW + timedelta(minutes=index),
                }
                for index in range(event_count)
            ],
        )


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
    postgres_database: Database,
) -> None:
    await _insert_owners_and_events(postgres_database)
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

    async with postgres_database.session() as session, session.begin():
        repository = NotificationRepository(session)
        for notification in notifications:
            await repository.add(notification)

    async with postgres_database.session() as session:
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

    assert simulated == notifications[0]
    assert failed == notifications[1]
    assert missing is None
    assert simulated is not None
    assert failed is not None
    assert isinstance(simulated.id, UUID)
    assert simulated.channel is NotificationChannel.EMAIL
    assert simulated.status is NotificationStatus.SIMULATED
    assert simulated.created_at.utcoffset() == timedelta(0)
    assert simulated.simulated_at is not None
    assert simulated.simulated_at.utcoffset() == timedelta(0)
    assert failed.status is NotificationStatus.FAILED
    assert failed.simulated_at is None
    assert failed.error_detail == "Notification rendering or simulation failed."

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
    postgres_database: Database,
) -> None:
    await _insert_owners_and_events(postgres_database, event_count=1)
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

    async with postgres_database.session() as session, session.begin():
        await NotificationRepository(session).add(original)

    with pytest.raises(IntegrityError) as error:
        async with postgres_database.session() as session, session.begin():
            await NotificationRepository(session).add(duplicate)

    diagnostic = getattr(error.value.orig, "diag", None)
    assert diagnostic is not None
    assert diagnostic.constraint_name == "uq_notifications_tracking_event_id"
    async with postgres_database.session() as session:
        count = await session.scalar(text("SELECT count(*) FROM notifications"))
        persisted = await NotificationRepository(session).get(original.id)
    assert count == 1
    assert persisted == original


async def test_foreign_keys_reject_missing_owners_and_restrict_deletion(
    postgres_database: Database,
) -> None:
    await _insert_owners_and_events(postgres_database, event_count=1)

    missing_shipment = _notification(
        0,
        status=NotificationStatus.SIMULATED,
        created_at=NOW,
        shipment_id=UUID(int=99_991),
    )
    with pytest.raises(IntegrityError) as shipment_error:
        async with postgres_database.session() as session, session.begin():
            await NotificationRepository(session).add(missing_shipment)
    shipment_diagnostic = getattr(shipment_error.value.orig, "diag", None)
    assert shipment_diagnostic is not None
    assert shipment_diagnostic.constraint_name == "fk_notifications_shipment_id_shipments"

    missing_tracking_event = _notification(
        1,
        status=NotificationStatus.SIMULATED,
        created_at=NOW,
        shipment_id=SHIPMENT_ID,
        tracking_event_id=UUID(int=99_992),
    )
    with pytest.raises(IntegrityError) as tracking_error:
        async with postgres_database.session() as session, session.begin():
            await NotificationRepository(session).add(missing_tracking_event)
    tracking_diagnostic = getattr(tracking_error.value.orig, "diag", None)
    assert tracking_diagnostic is not None
    assert (
        tracking_diagnostic.constraint_name == "fk_notifications_tracking_event_id_tracking_events"
    )

    notification = _notification(
        2,
        status=NotificationStatus.SIMULATED,
        created_at=NOW,
        shipment_id=SECOND_SHIPMENT_ID,
        tracking_event_id=TRACKING_EVENT_IDS[0],
    )
    async with postgres_database.session() as session, session.begin():
        await NotificationRepository(session).add(notification)

    with pytest.raises(IntegrityError) as shipment_delete_error:
        async with postgres_database.session() as session, session.begin():
            await session.execute(
                text("DELETE FROM shipments WHERE id = :id"),
                {"id": SECOND_SHIPMENT_ID},
            )
    shipment_delete_diagnostic = getattr(shipment_delete_error.value.orig, "diag", None)
    assert shipment_delete_diagnostic is not None
    assert shipment_delete_diagnostic.constraint_name == "fk_notifications_shipment_id_shipments"

    with pytest.raises(IntegrityError) as tracking_delete_error:
        async with postgres_database.session() as session, session.begin():
            await session.execute(
                text("DELETE FROM tracking_events WHERE id = :id"),
                {"id": TRACKING_EVENT_IDS[0]},
            )
    tracking_delete_diagnostic = getattr(tracking_delete_error.value.orig, "diag", None)
    assert tracking_delete_diagnostic is not None
    assert (
        tracking_delete_diagnostic.constraint_name
        == "fk_notifications_tracking_event_id_tracking_events"
    )

    async with postgres_database.session() as session:
        delete_actions = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname, confdeltype::text FROM pg_constraint "
                        "WHERE conname IN "
                        "('fk_notifications_shipment_id_shipments', "
                        "'fk_notifications_tracking_event_id_tracking_events')"
                    )
                )
            )
            .tuples()
            .all()
        )
    assert delete_actions == {
        "fk_notifications_shipment_id_shipments": "r",
        "fk_notifications_tracking_event_id_tracking_events": "r",
    }


async def test_database_enforces_notification_enums_nonempty_content_and_index(
    postgres_database: Database,
) -> None:
    await _insert_owners_and_events(postgres_database, event_count=1)
    notification = _notification(
        0,
        status=NotificationStatus.SIMULATED,
        created_at=NOW,
    )
    async with postgres_database.session() as session, session.begin():
        await NotificationRepository(session).add(notification)

    invalid_updates = (
        ("channel", "SMS", "ck_notifications_notification_channel"),
        ("status", "PENDING", "ck_notifications_notification_status"),
        ("recipient", "   ", "ck_notifications_recipient_nonempty"),
        ("template_key", "   ", "ck_notifications_template_key_nonempty"),
        ("message", "   ", "ck_notifications_message_nonempty"),
    )
    for field, value, constraint_name in invalid_updates:
        with pytest.raises(IntegrityError) as error:
            async with postgres_database.session() as session, session.begin():
                await session.execute(
                    text(f"UPDATE notifications SET {field} = :value WHERE id = :id"),
                    {"id": notification.id, "value": value},
                )
        diagnostic = getattr(error.value.orig, "diag", None)
        assert diagnostic is not None
        assert diagnostic.constraint_name == constraint_name

    async with postgres_database.session() as session:
        persisted = await NotificationRepository(session).get(notification.id)
        index_definition = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'ix_notifications_status_created_at'"
            )
        )
    assert persisted == notification
    assert index_definition is not None
    assert "(status, created_at DESC)" in index_definition

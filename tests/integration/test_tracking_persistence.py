"""Real PostgreSQL checks for Tracking-owned persistence and constraints."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fulfillflow.db import Database
from fulfillflow.tracking.domain import (
    CarrierEventInbox,
    InboxStatus,
    ShipmentApplicationResult,
    ShipmentStatus,
    TrackingEvent,
)
from fulfillflow.tracking.repository import InboxFilters, TrackingRepository

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
CARRIER_ID = UUID("00000000-0000-4000-8000-000000000910")
ORDER_ID = UUID("00000000-0000-4000-8000-000000000911")
SHIPMENT_ID = UUID("00000000-0000-4000-8000-000000000912")
INBOX_ID = UUID("00000000-0000-4000-8000-000000000913")
TRACKING_EVENT_ID = UUID("00000000-0000-4000-8000-000000000914")
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000915")
PAYLOAD_SHA256 = "a" * 64


async def _insert_owners(database: Database) -> None:
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO carriers "
                "(id, code, name, adapter_key, active, created_at, updated_at) "
                "VALUES (:id, 'tracking-persistence', 'Tracking Persistence', "
                "'tracking-persistence', true, :now, :now)"
            ),
            {"id": CARRIER_ID, "now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO orders "
                "(id, external_reference, recipient_name, recipient_email, "
                "recipient_postal_code, recipient_city, recipient_state, status, "
                "created_at, updated_at) VALUES "
                "(:id, 'TRACKING-PERSISTENCE', 'Tracking Recipient', "
                "'tracking@example.test', '09700-000', 'Sao Bernardo do Campo', "
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
                "VALUES (:id, :order_id, :carrier_id, 'TRACK-PERSISTENCE', 'POSTED', "
                ":now, NULL, NULL, NULL, :now, NULL, :now, :now)"
            ),
            {
                "id": SHIPMENT_ID,
                "order_id": ORDER_ID,
                "carrier_id": CARRIER_ID,
                "now": NOW,
            },
        )


def _inbox(*, identifier: UUID = INBOX_ID, raw_body: bytes) -> CarrierEventInbox:
    return CarrierEventInbox(
        id=identifier,
        carrier_id=CARRIER_ID,
        external_event_id="evt-byte-exact-001",
        payload_sha256=PAYLOAD_SHA256,
        raw_body=raw_body,
        parsed_payload={"tracking_code": "TRACK-PERSISTENCE", "status": "in_transit"},
        received_at=NOW,
        status=InboxStatus.RECEIVED,
        error_code=None,
        error_detail=None,
        processed_at=None,
        request_id=REQUEST_ID,
    )


async def test_repository_preserves_raw_bytes_and_maps_the_append_only_timeline(
    postgres_database: Database,
) -> None:
    await _insert_owners(postgres_database)
    raw_body = b'{\r\n  "status": "in_transit", "tracking_code": "TRACK-PERSISTENCE"\r\n}'

    async with postgres_database.session() as session, session.begin():
        await TrackingRepository(session).add_inbox(_inbox(raw_body=raw_body))

    processed_at = NOW + timedelta(seconds=1)
    async with postgres_database.session() as session, session.begin():
        repository = TrackingRepository(session)
        persisted_inbox = await repository.get_inbox(INBOX_ID, for_update=True)
        assert persisted_inbox is not None
        assert persisted_inbox.raw_body == raw_body
        assert persisted_inbox.parsed_payload == {
            "tracking_code": "TRACK-PERSISTENCE",
            "status": "in_transit",
        }
        persisted_inbox.mark_processed(processed_at)
        await repository.save_inbox(persisted_inbox)
        await repository.add_tracking_event(
            TrackingEvent(
                id=TRACKING_EVENT_ID,
                inbox_event_id=INBOX_ID,
                shipment_id=SHIPMENT_ID,
                carrier_id=CARRIER_ID,
                external_status="in_transit",
                canonical_status=ShipmentStatus.IN_TRANSIT,
                description="Shipment moving",
                location="Sao Paulo, SP",
                occurred_at=NOW - timedelta(minutes=1),
                received_at=NOW,
                application_result=ShipmentApplicationResult.APPLIED,
                previous_shipment_status=ShipmentStatus.POSTED,
                resulting_shipment_status=ShipmentStatus.IN_TRANSIT,
                created_at=NOW,
            )
        )

    async with postgres_database.session() as session:
        repository = TrackingRepository(session)
        persisted_inbox = await repository.get_inbox_by_carrier_event(
            CARRIER_ID,
            "evt-byte-exact-001",
        )
        persisted_event = await repository.get_tracking_event_by_inbox(INBOX_ID)
        timeline = await repository.timeline(SHIPMENT_ID, page=1, page_size=25)
        page = await repository.list_inbox(
            InboxFilters(carrier_id=CARRIER_ID, status=InboxStatus.PROCESSED),
            page=1,
            page_size=25,
        )

    assert persisted_inbox is not None
    assert persisted_inbox.raw_body == raw_body
    assert persisted_inbox.status is InboxStatus.PROCESSED
    assert persisted_inbox.processed_at == processed_at
    assert persisted_event is not None
    assert persisted_event.application_result is ShipmentApplicationResult.APPLIED
    assert persisted_event.canonical_status is ShipmentStatus.IN_TRANSIT
    assert timeline.items == [persisted_event]
    assert timeline.total == 1
    assert page.items == [persisted_inbox]
    assert page.total == 1


async def test_database_enforces_inbox_idempotency_and_raw_body_limit(
    postgres_database: Database,
) -> None:
    await _insert_owners(postgres_database)
    async with postgres_database.session() as session, session.begin():
        await TrackingRepository(session).add_inbox(_inbox(raw_body=b"{}"))

    with pytest.raises(IntegrityError) as duplicate:
        async with postgres_database.session() as session, session.begin():
            await TrackingRepository(session).add_inbox(
                _inbox(identifier=UUID(int=916), raw_body=b'{"different":true}')
            )
    duplicate_diagnostic = getattr(duplicate.value.orig, "diag", None)
    assert duplicate_diagnostic is not None
    assert (
        duplicate_diagnostic.constraint_name == "uq_carrier_event_inbox_carrier_external_event_id"
    )

    with pytest.raises(IntegrityError) as oversized:
        async with postgres_database.session() as session, session.begin():
            oversized_inbox = _inbox(identifier=UUID(int=917), raw_body=b"x" * 65_537)
            oversized_inbox.external_event_id = "evt-oversized-001"
            await TrackingRepository(session).add_inbox(oversized_inbox)
    oversized_diagnostic = getattr(oversized.value.orig, "diag", None)
    assert oversized_diagnostic is not None
    assert oversized_diagnostic.constraint_name == "ck_carrier_event_inbox_raw_body_max_64_kib"


async def test_database_enforces_one_tracking_event_per_inbox(
    postgres_database: Database,
) -> None:
    await _insert_owners(postgres_database)
    async with postgres_database.session() as session, session.begin():
        repository = TrackingRepository(session)
        await repository.add_inbox(_inbox(raw_body=b"{}"))
        await repository.add_tracking_event(
            TrackingEvent(
                id=TRACKING_EVENT_ID,
                inbox_event_id=INBOX_ID,
                shipment_id=SHIPMENT_ID,
                carrier_id=CARRIER_ID,
                external_status="in_transit",
                canonical_status=ShipmentStatus.IN_TRANSIT,
                description=None,
                location=None,
                occurred_at=NOW,
                received_at=NOW,
                application_result=ShipmentApplicationResult.APPLIED,
                previous_shipment_status=ShipmentStatus.POSTED,
                resulting_shipment_status=ShipmentStatus.IN_TRANSIT,
                created_at=NOW,
            )
        )

    with pytest.raises(IntegrityError) as duplicate:
        async with postgres_database.session() as session, session.begin():
            await TrackingRepository(session).add_tracking_event(
                TrackingEvent(
                    id=UUID("00000000-0000-4000-8000-000000000918"),
                    inbox_event_id=INBOX_ID,
                    shipment_id=SHIPMENT_ID,
                    carrier_id=CARRIER_ID,
                    external_status="delivered",
                    canonical_status=ShipmentStatus.DELIVERED,
                    description=None,
                    location=None,
                    occurred_at=NOW + timedelta(minutes=1),
                    received_at=NOW + timedelta(minutes=1),
                    application_result=ShipmentApplicationResult.APPLIED,
                    previous_shipment_status=ShipmentStatus.IN_TRANSIT,
                    resulting_shipment_status=ShipmentStatus.DELIVERED,
                    created_at=NOW + timedelta(minutes=1),
                )
            )

    duplicate_diagnostic = getattr(duplicate.value.orig, "diag", None)
    assert duplicate_diagnostic is not None
    assert duplicate_diagnostic.constraint_name == "uq_tracking_events_inbox_event_id"

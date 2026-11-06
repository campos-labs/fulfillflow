"""Isolated PostgreSQL migration, constraint and readiness integration tests."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.main import create_app

BUSINESS_TABLES = {
    "orders",
    "carriers",
    "shipments",
    "carrier_event_inbox",
    "tracking_events",
    "notifications",
}
PRE_NOTIFICATION_TABLES = BUSINESS_TABLES - {"notifications"}
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
ALPHA_ID = UUID("00000000-0000-4000-8000-000000000100")
BETA_ID = UUID("00000000-0000-4000-8000-000000000101")
ORDER_ID = UUID("00000000-0000-4000-8000-000000000201")
CARRIER_ID = UUID("00000000-0000-4000-8000-000000000202")
SHIPMENT_ID = UUID("00000000-0000-4000-8000-000000000203")
INBOX_ID = UUID("00000000-0000-4000-8000-000000000204")
TRACKING_EVENT_ID = UUID("00000000-0000-4000-8000-000000000205")
NOTIFICATION_ID = UUID("00000000-0000-4000-8000-000000000206")

ORDER_INSERT = """
INSERT INTO orders (
    id, external_reference, recipient_name, recipient_email,
    recipient_postal_code, recipient_city, recipient_state,
    status, created_at, updated_at
) VALUES (
    :id, :external_reference, :recipient_name, :recipient_email,
    :recipient_postal_code, :recipient_city, :recipient_state,
    :status, :created_at, :updated_at
)
"""
CARRIER_INSERT = """
INSERT INTO carriers (id, code, name, adapter_key, active, created_at, updated_at)
VALUES (:id, :code, :name, :adapter_key, :active, :created_at, :updated_at)
"""
SHIPMENT_INSERT = """
INSERT INTO shipments (
    id, order_id, carrier_id, tracking_code, status,
    status_occurred_at, status_event_received_at, status_external_event_id,
    estimated_delivery_date, shipped_at, delivered_at, created_at, updated_at
) VALUES (
    :id, :order_id, :carrier_id, :tracking_code, :status,
    :status_occurred_at, :status_event_received_at, :status_external_event_id,
    :estimated_delivery_date, :shipped_at, :delivered_at, :created_at, :updated_at
)
"""
INBOX_INSERT = """
INSERT INTO carrier_event_inbox (
    id, carrier_id, external_event_id, payload_sha256, raw_body,
    parsed_payload, received_at, status, error_code, error_detail,
    processed_at, request_id
) VALUES (
    :id, :carrier_id, :external_event_id, :payload_sha256, :raw_body,
    :parsed_payload, :received_at, :status, :error_code, :error_detail,
    :processed_at, :request_id
)
"""
TRACKING_EVENT_INSERT = """
INSERT INTO tracking_events (
    id, inbox_event_id, shipment_id, carrier_id, external_status,
    canonical_status, description, location, occurred_at, received_at,
    application_result, previous_shipment_status, resulting_shipment_status,
    created_at
) VALUES (
    :id, :inbox_event_id, :shipment_id, :carrier_id, :external_status,
    :canonical_status, :description, :location, :occurred_at, :received_at,
    :application_result, :previous_shipment_status, :resulting_shipment_status,
    :created_at
)
"""
NOTIFICATION_INSERT = """
INSERT INTO notifications (
    id, shipment_id, tracking_event_id, channel, recipient,
    template_key, message, status, error_detail, created_at, simulated_at
) VALUES (
    :id, :shipment_id, :tracking_event_id, :channel, :recipient,
    :template_key, :message, :status, :error_detail, :created_at, :simulated_at
)
"""


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        session_secret="integration-session",
        carrier_alpha_webhook_secret="integration-alpha",
        carrier_beta_webhook_secret="integration-beta",
    )


def _order_parameters(identifier: UUID, reference: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "external_reference": reference,
        "recipient_name": "Migration Recipient",
        "recipient_email": "migration@example.test",
        "recipient_postal_code": "09700-000",
        "recipient_city": "Sao Bernardo do Campo",
        "recipient_state": "SP",
        "status": "CONFIRMED",
        "created_at": NOW,
        "updated_at": NOW,
    }


def _carrier_parameters(
    identifier: UUID,
    code: str,
    adapter_key: str,
    *,
    name: str = "Migration Carrier",
    active: bool = True,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "code": code,
        "name": name,
        "adapter_key": adapter_key,
        "active": active,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _shipment_parameters(
    identifier: UUID,
    *,
    order_id: UUID = ORDER_ID,
    carrier_id: UUID = CARRIER_ID,
    tracking_code: str,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "order_id": order_id,
        "carrier_id": carrier_id,
        "tracking_code": tracking_code,
        "status": "PENDING",
        "status_occurred_at": NOW,
        "status_event_received_at": None,
        "status_external_event_id": None,
        "estimated_delivery_date": None,
        "shipped_at": None,
        "delivered_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _inbox_parameters(identifier: UUID = INBOX_ID) -> dict[str, Any]:
    return {
        "id": identifier,
        "carrier_id": CARRIER_ID,
        "external_event_id": f"migration-{identifier}",
        "payload_sha256": "0" * 64,
        "raw_body": b"{}",
        "parsed_payload": None,
        "received_at": NOW,
        "status": "PROCESSED",
        "error_code": None,
        "error_detail": None,
        "processed_at": NOW,
        "request_id": UUID(int=800),
    }


def _tracking_event_parameters(
    identifier: UUID = TRACKING_EVENT_ID,
    *,
    inbox_event_id: UUID = INBOX_ID,
    shipment_id: UUID = SHIPMENT_ID,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "inbox_event_id": inbox_event_id,
        "shipment_id": shipment_id,
        "carrier_id": CARRIER_ID,
        "external_status": "CREATED",
        "canonical_status": "POSTED",
        "description": None,
        "location": None,
        "occurred_at": NOW,
        "received_at": NOW,
        "application_result": "APPLIED",
        "previous_shipment_status": "PENDING",
        "resulting_shipment_status": "POSTED",
        "created_at": NOW,
    }


def _notification_parameters(
    identifier: UUID = NOTIFICATION_ID,
    *,
    shipment_id: UUID = SHIPMENT_ID,
    tracking_event_id: UUID = TRACKING_EVENT_ID,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "shipment_id": shipment_id,
        "tracking_event_id": tracking_event_id,
        "channel": "EMAIL",
        "recipient": "migration@example.test",
        "template_key": "shipment_posted",
        "message": "Shipment status changed to POSTED.",
        "status": "SIMULATED",
        "error_detail": None,
        "created_at": NOW,
        "simulated_at": NOW,
    }


async def _revision_and_business_tables(database: Database) -> tuple[str | None, set[str]]:
    async with database.engine.connect() as connection:
        revision = await connection.run_sync(
            lambda sync_connection: MigrationContext.configure(
                sync_connection
            ).get_current_revision()
        )
        tables = set(
            (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name IN "
                        "('orders', 'carriers', 'shipments', "
                        "'carrier_event_inbox', 'tracking_events', 'notifications')"
                    )
                )
            ).scalars()
        )
    return revision, tables


async def _reference_carriers(
    database: Database,
) -> dict[str, tuple[UUID, str, str, bool]]:
    async with database.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT code, id, name, adapter_key, active FROM carriers "
                    "WHERE code IN ('carrier-alpha', 'carrier-beta') ORDER BY code"
                )
            )
        ).all()
    return {row.code: (row.id, row.name, row.adapter_key, row.active) for row in rows}


async def _verify_tracking_downgrade_retains_business_data(database: Database) -> None:
    revision, tables = await _revision_and_business_tables(database)
    assert revision == "0002_orders_shipments"
    assert tables == {"orders", "carriers", "shipments"}
    async with database.engine.connect() as connection:
        carriers = await connection.scalar(text("SELECT count(*) FROM carriers"))
        shipments = await connection.scalar(text("SELECT count(*) FROM shipments"))
    assert carriers == 3
    assert shipments == 1


async def _verify_notification_downgrade_retains_prior_data(database: Database) -> None:
    revision, tables = await _revision_and_business_tables(database)
    assert revision == "0003_carriers_tracking"
    assert tables == PRE_NOTIFICATION_TABLES
    async with database.engine.connect() as connection:
        orders = await connection.scalar(text("SELECT count(*) FROM orders"))
        shipments = await connection.scalar(text("SELECT count(*) FROM shipments"))
        inbox_events = await connection.scalar(text("SELECT count(*) FROM carrier_event_inbox"))
        tracking_events = await connection.scalar(text("SELECT count(*) FROM tracking_events"))
    assert orders == shipments == inbox_events == tracking_events == 1


async def _insert_preexisting_reference_scenario(database: Database) -> UUID:
    beta_alternate_id = UUID("00000000-0000-4000-8000-000000000700")
    async with database.engine.begin() as connection:
        await connection.execute(
            text(CARRIER_INSERT),
            [
                _carrier_parameters(
                    ALPHA_ID,
                    "carrier-alpha",
                    "alpha",
                    name="Custom Alpha Name",
                    active=False,
                ),
                _carrier_parameters(
                    beta_alternate_id,
                    "carrier-beta",
                    "beta",
                    name="Custom Beta Name",
                ),
            ],
        )
        await connection.execute(
            text(ORDER_INSERT),
            _order_parameters(ORDER_ID, "ORDER-PREEXISTING-CARRIER"),
        )
        await connection.execute(
            text(SHIPMENT_INSERT),
            _shipment_parameters(
                SHIPMENT_ID,
                carrier_id=beta_alternate_id,
                tracking_code="PREEXISTING-CARRIER",
            ),
        )
    return beta_alternate_id


async def _assert_preexisting_reference_scenario(
    database: Database,
    beta_alternate_id: UUID,
) -> None:
    assert await _reference_carriers(database) == {
        "carrier-alpha": (ALPHA_ID, "Custom Alpha Name", "alpha", False),
        "carrier-beta": (beta_alternate_id, "Custom Beta Name", "beta", True),
    }
    async with database.engine.connect() as connection:
        shipment_carrier_id = await connection.scalar(
            text("SELECT carrier_id FROM shipments WHERE id = :id"),
            {"id": SHIPMENT_ID},
        )
    assert shipment_carrier_id == beta_alternate_id


async def _assert_rejected(
    database: Database,
    statement: str,
    parameters: dict[str, Any],
    constraint_name: str,
) -> None:
    with pytest.raises(IntegrityError) as raised:
        async with database.engine.begin() as connection:
            await connection.execute(text(statement), parameters)

    diagnostic = getattr(raised.value.orig, "diag", None)
    assert diagnostic is not None
    assert diagnostic.constraint_name == constraint_name


async def _verify_head_schema_and_constraints(settings: Settings) -> None:
    database = Database.from_settings(settings)
    try:
        async with database.engine.connect() as connection:
            server_version = int(
                (await connection.execute(text("SHOW server_version_num"))).scalar_one()
            )
            indexes = set(
                (
                    await connection.execute(
                        text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE schemaname = 'public' "
                            "AND tablename IN ('orders', 'shipments', "
                            "'carrier_event_inbox', 'tracking_events', 'notifications')"
                        )
                    )
                ).scalars()
            )
            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid IN "
                            "('orders'::regclass, 'carriers'::regclass, "
                            "'shipments'::regclass, 'carrier_event_inbox'::regclass, "
                            "'tracking_events'::regclass, 'notifications'::regclass)"
                        )
                    )
                ).scalars()
            )
            delete_actions = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT conname, confdeltype FROM pg_constraint "
                            "WHERE conname IN "
                            "('fk_shipments_order_id_orders', "
                            "'fk_shipments_carrier_id_carriers', "
                            "'fk_carrier_event_inbox_carrier_id_carriers', "
                            "'fk_tracking_events_carrier_id_carriers', "
                            "'fk_tracking_events_inbox_event_id_carrier_event_inbox', "
                            "'fk_tracking_events_shipment_id_shipments', "
                            "'fk_notifications_shipment_id_shipments', "
                            "'fk_notifications_tracking_event_id_tracking_events')"
                        )
                    )
                )
                .tuples()
                .all()
            )
            column_types = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name || '.' || column_name, data_type "
                            "FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND "
                            "(table_name, column_name) IN "
                            "(( 'orders', 'id'), ('orders', 'status'), "
                            "('orders', 'created_at'), ('shipments', 'id'), "
                            "('shipments', 'status_occurred_at'), "
                            "('carrier_event_inbox', 'raw_body'), "
                            "('carrier_event_inbox', 'parsed_payload'), "
                            "('tracking_events', 'occurred_at'), "
                            "('notifications', 'id'), "
                            "('notifications', 'created_at'), "
                            "('notifications', 'simulated_at'))"
                        )
                    )
                )
                .tuples()
                .all()
            )

        assert 180000 <= server_version < 190000
        assert {
            "uq_orders_external_reference",
            "ix_orders_status_created_at",
            "uq_shipments_carrier_tracking_code",
            "ix_shipments_order_id",
            "ix_shipments_status_updated_at",
            "uq_carrier_event_inbox_carrier_external_event_id",
            "ix_carrier_event_inbox_status_received_at",
            "uq_tracking_events_inbox_event_id",
            "ix_tracking_events_shipment_occurred_at_created_at",
            "uq_notifications_tracking_event_id",
            "ix_notifications_status_created_at",
        }.issubset(indexes)
        assert {
            "ck_orders_order_status",
            "ck_shipments_shipment_status",
            "ck_shipments_tracking_code_normalized",
            "uq_orders_external_reference",
            "uq_shipments_carrier_tracking_code",
            "ck_carrier_event_inbox_inbox_status",
            "ck_carrier_event_inbox_raw_body_max_64_kib",
            "ck_tracking_events_application_result",
            "uq_carrier_event_inbox_carrier_external_event_id",
            "uq_tracking_events_inbox_event_id",
            "ck_notifications_notification_channel",
            "ck_notifications_notification_status",
            "ck_notifications_recipient_nonempty",
            "ck_notifications_template_key_nonempty",
            "ck_notifications_message_nonempty",
            "uq_notifications_tracking_event_id",
        }.issubset(constraints)
        assert delete_actions == {
            "fk_shipments_carrier_id_carriers": "r",
            "fk_shipments_order_id_orders": "r",
            "fk_carrier_event_inbox_carrier_id_carriers": "r",
            "fk_tracking_events_carrier_id_carriers": "r",
            "fk_tracking_events_inbox_event_id_carrier_event_inbox": "r",
            "fk_tracking_events_shipment_id_shipments": "r",
            "fk_notifications_shipment_id_shipments": "r",
            "fk_notifications_tracking_event_id_tracking_events": "r",
        }
        assert column_types == {
            "orders.created_at": "timestamp with time zone",
            "orders.id": "uuid",
            "orders.status": "character varying",
            "shipments.id": "uuid",
            "shipments.status_occurred_at": "timestamp with time zone",
            "carrier_event_inbox.raw_body": "bytea",
            "carrier_event_inbox.parsed_payload": "jsonb",
            "tracking_events.occurred_at": "timestamp with time zone",
            "notifications.id": "uuid",
            "notifications.created_at": "timestamp with time zone",
            "notifications.simulated_at": "timestamp with time zone",
        }

        reference_carriers = await _reference_carriers(database)
        assert reference_carriers == {
            "carrier-alpha": (ALPHA_ID, "Carrier Alpha", "alpha", True),
            "carrier-beta": (BETA_ID, "Carrier Beta", "beta", True),
        }

        async with database.engine.begin() as connection:
            await connection.execute(
                text(ORDER_INSERT),
                _order_parameters(ORDER_ID, "ORDER-MIGRATION-VALID"),
            )
            await connection.execute(
                text(CARRIER_INSERT),
                _carrier_parameters(CARRIER_ID, "migration-carrier", "migration"),
            )
            await connection.execute(
                text(SHIPMENT_INSERT),
                _shipment_parameters(SHIPMENT_ID, tracking_code="MIGRATION-TRACK"),
            )
            await connection.execute(text(INBOX_INSERT), _inbox_parameters())
            await connection.execute(
                text(TRACKING_EVENT_INSERT),
                _tracking_event_parameters(),
            )
            await connection.execute(
                text(NOTIFICATION_INSERT),
                _notification_parameters(),
            )

        order_checks = [
            ("ck_orders_order_status", {"status": "UNKNOWN"}),
            ("ck_orders_external_reference_nonempty", {"external_reference": "   "}),
            ("ck_orders_recipient_name_nonempty", {"recipient_name": "   "}),
            ("ck_orders_recipient_email_nonempty", {"recipient_email": "   "}),
            (
                "ck_orders_recipient_postal_code_nonempty",
                {"recipient_postal_code": "   "},
            ),
            ("ck_orders_recipient_city_nonempty", {"recipient_city": "   "}),
            ("ck_orders_recipient_state_upper", {"recipient_state": "sp"}),
        ]
        for offset, (constraint_name, override) in enumerate(order_checks, start=1):
            parameters = _order_parameters(
                UUID(int=300 + offset),
                f"ORDER-MIGRATION-CHECK-{offset}",
            )
            parameters.update(override)
            await _assert_rejected(database, ORDER_INSERT, parameters, constraint_name)

        carrier_checks = [
            ("ck_carriers_carrier_code_slug", {"code": "Invalid Code"}),
            ("ck_carriers_carrier_name_nonempty", {"name": "   "}),
            ("ck_carriers_adapter_key_nonempty", {"adapter_key": "   "}),
        ]
        for offset, (constraint_name, override) in enumerate(carrier_checks, start=1):
            parameters = _carrier_parameters(
                UUID(int=400 + offset),
                f"migration-carrier-{offset}",
                f"migration-{offset}",
            )
            parameters.update(override)
            await _assert_rejected(database, CARRIER_INSERT, parameters, constraint_name)

        shipment_checks = [
            ("ck_shipments_shipment_status", {"status": "UNKNOWN"}),
            (
                "ck_shipments_tracking_code_normalized",
                {"tracking_code": " not-normalized "},
            ),
            (
                "ck_shipments_external_ordering_complete",
                {"status_event_received_at": NOW},
            ),
            (
                "ck_shipments_external_event_id_nonempty",
                {"status_event_received_at": NOW, "status_external_event_id": ""},
            ),
        ]
        for offset, (constraint_name, override) in enumerate(shipment_checks, start=1):
            parameters = _shipment_parameters(
                UUID(int=500 + offset),
                tracking_code=f"MIGRATION-CHECK-{offset}",
            )
            parameters.update(override)
            await _assert_rejected(database, SHIPMENT_INSERT, parameters, constraint_name)

        await _assert_rejected(
            database,
            "UPDATE notifications SET channel = :value WHERE id = :id",
            {"id": NOTIFICATION_ID, "value": "SMS"},
            "ck_notifications_notification_channel",
        )
        await _assert_rejected(
            database,
            "UPDATE notifications SET status = :value WHERE id = :id",
            {"id": NOTIFICATION_ID, "value": "PENDING"},
            "ck_notifications_notification_status",
        )
        for field, constraint_name in (
            ("recipient", "ck_notifications_recipient_nonempty"),
            ("template_key", "ck_notifications_template_key_nonempty"),
            ("message", "ck_notifications_message_nonempty"),
        ):
            await _assert_rejected(
                database,
                f"UPDATE notifications SET {field} = :value WHERE id = :id",
                {"id": NOTIFICATION_ID, "value": "   "},
                constraint_name,
            )
        await _assert_rejected(
            database,
            NOTIFICATION_INSERT,
            _notification_parameters(UUID(int=607)),
            "uq_notifications_tracking_event_id",
        )
        await _assert_rejected(
            database,
            "UPDATE notifications SET shipment_id = :value WHERE id = :id",
            {"id": NOTIFICATION_ID, "value": UUID(int=9993)},
            "fk_notifications_shipment_id_shipments",
        )
        await _assert_rejected(
            database,
            "UPDATE notifications SET tracking_event_id = :value WHERE id = :id",
            {"id": NOTIFICATION_ID, "value": UUID(int=9994)},
            "fk_notifications_tracking_event_id_tracking_events",
        )

        await _assert_rejected(
            database,
            ORDER_INSERT,
            _order_parameters(UUID(int=601), "ORDER-MIGRATION-VALID"),
            "uq_orders_external_reference",
        )
        await _assert_rejected(
            database,
            CARRIER_INSERT,
            _carrier_parameters(UUID(int=602), "migration-carrier", "migration-other"),
            "uq_carriers_code",
        )
        await _assert_rejected(
            database,
            CARRIER_INSERT,
            _carrier_parameters(UUID(int=603), "migration-other", "migration"),
            "uq_carriers_adapter_key",
        )
        await _assert_rejected(
            database,
            SHIPMENT_INSERT,
            _shipment_parameters(UUID(int=604), tracking_code="MIGRATION-TRACK"),
            "uq_shipments_carrier_tracking_code",
        )
        await _assert_rejected(
            database,
            SHIPMENT_INSERT,
            _shipment_parameters(
                UUID(int=605),
                order_id=UUID(int=9991),
                tracking_code="MISSING-ORDER",
            ),
            "fk_shipments_order_id_orders",
        )
        await _assert_rejected(
            database,
            SHIPMENT_INSERT,
            _shipment_parameters(
                UUID(int=606),
                carrier_id=UUID(int=9992),
                tracking_code="MISSING-CARRIER",
            ),
            "fk_shipments_carrier_id_carriers",
        )
        await _assert_rejected(
            database,
            "DELETE FROM orders WHERE id = :id",
            {"id": ORDER_ID},
            "fk_shipments_order_id_orders",
        )
        await _assert_rejected(
            database,
            "DELETE FROM carriers WHERE id = :id",
            {"id": CARRIER_ID},
            "fk_shipments_carrier_id_carriers",
        )
        await _assert_rejected(
            database,
            "DELETE FROM tracking_events WHERE id = :id",
            {"id": TRACKING_EVENT_ID},
            "fk_notifications_tracking_event_id_tracking_events",
        )
    finally:
        await database.dispose()


async def _verify_application_readiness(settings: Settings) -> None:
    application = create_app(settings, alembic_config_path=Path("alembic.ini"))
    async with application.router.lifespan_context(application):
        database = cast(Database, application.state.database)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            live_response = await client.get("/health/live")
            ready_response = await client.get("/health/ready")
        assert database is not None

    assert live_response.status_code == 200
    assert ready_response.status_code == 200
    assert ready_response.json() == {"status": "ok"}


@pytest.mark.integration
def test_migrations_are_isolated_reversible_and_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("TEST_LEGACY_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_LEGACY_DATABASE_URL must point to a dedicated PostgreSQL 18 database")

    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    settings = _settings(database_url)

    migration_error: BaseException | None = None
    try:
        command.downgrade(config, "base")
        base_database = Database.from_settings(settings)
        try:
            revision, tables = run_async(_revision_and_business_tables(base_database))
        finally:
            run_async(base_database.dispose())
        assert revision is None
        assert tables == set()

        command.upgrade(config, "0001_bootstrap")
        bootstrap_database = Database.from_settings(settings)
        try:
            revision, tables = run_async(_revision_and_business_tables(bootstrap_database))
        finally:
            run_async(bootstrap_database.dispose())
        assert revision == "0001_bootstrap"
        assert tables == set()

        command.upgrade(config, "0002_orders_shipments")
        command.upgrade(config, "0003_carriers_tracking")
        command.upgrade(config, "0004_notifications")
        run_async(_verify_head_schema_and_constraints(settings))
        run_async(_verify_application_readiness(settings))

        command.downgrade(config, "0003_carriers_tracking")
        notification_downgrade_database = Database.from_settings(settings)
        try:
            run_async(
                _verify_notification_downgrade_retains_prior_data(notification_downgrade_database)
            )
        finally:
            run_async(notification_downgrade_database.dispose())
        command.upgrade(config, "0004_notifications")

        command.downgrade(config, "0002_orders_shipments")
        retained_database = Database.from_settings(settings)
        try:
            run_async(_verify_tracking_downgrade_retains_business_data(retained_database))
        finally:
            run_async(retained_database.dispose())
        command.upgrade(config, "0003_carriers_tracking")
        command.upgrade(config, "0004_notifications")
        roundtrip_database = Database.from_settings(settings)
        try:
            revision, tables = run_async(_revision_and_business_tables(roundtrip_database))
            reference_carriers = run_async(_reference_carriers(roundtrip_database))
        finally:
            run_async(roundtrip_database.dispose())
        assert revision == "0004_notifications"
        assert tables == BUSINESS_TABLES
        assert reference_carriers == {
            "carrier-alpha": (ALPHA_ID, "Carrier Alpha", "alpha", True),
            "carrier-beta": (BETA_ID, "Carrier Beta", "beta", True),
        }

        command.downgrade(config, "0001_bootstrap")
        downgraded_database = Database.from_settings(settings)
        try:
            revision, tables = run_async(_revision_and_business_tables(downgraded_database))
        finally:
            run_async(downgraded_database.dispose())
        assert revision == "0001_bootstrap"
        assert tables == set()
    except BaseException as error:
        migration_error = error
        raise
    finally:
        try:
            command.upgrade(config, "head")
        except BaseException as restoration_error:
            if migration_error is None:
                raise
            migration_error.add_note(
                f"Restoring the database migration to head also failed: {restoration_error!r}"
            )

    restored_database = Database.from_settings(settings)
    try:
        revision, tables = run_async(_revision_and_business_tables(restored_database))
    finally:
        run_async(restored_database.dispose())
    assert revision == "0004_notifications"
    assert tables == BUSINESS_TABLES


@pytest.mark.integration
def test_0003_preserves_preexisting_carriers_shipments_and_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("TEST_LEGACY_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_LEGACY_DATABASE_URL must point to a dedicated PostgreSQL 18 database")

    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    settings = _settings(database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0002_orders_shipments")
        setup_database = Database.from_settings(settings)
        try:
            beta_alternate_id = run_async(_insert_preexisting_reference_scenario(setup_database))
        finally:
            run_async(setup_database.dispose())

        command.upgrade(config, "0003_carriers_tracking")
        upgraded_database = Database.from_settings(settings)
        try:
            run_async(
                _assert_preexisting_reference_scenario(
                    upgraded_database,
                    beta_alternate_id,
                )
            )
        finally:
            run_async(upgraded_database.dispose())

        command.downgrade(config, "0002_orders_shipments")
        downgraded_database = Database.from_settings(settings)
        try:
            revision, tables = run_async(_revision_and_business_tables(downgraded_database))
            run_async(
                _assert_preexisting_reference_scenario(
                    downgraded_database,
                    beta_alternate_id,
                )
            )
        finally:
            run_async(downgraded_database.dispose())
        assert revision == "0002_orders_shipments"
        assert tables == {"orders", "carriers", "shipments"}

        command.upgrade(config, "0003_carriers_tracking")
        roundtrip_database = Database.from_settings(settings)
        try:
            revision, tables = run_async(_revision_and_business_tables(roundtrip_database))
            run_async(
                _assert_preexisting_reference_scenario(
                    roundtrip_database,
                    beta_alternate_id,
                )
            )
        finally:
            run_async(roundtrip_database.dispose())
        assert revision == "0003_carriers_tracking"
        assert tables == PRE_NOTIFICATION_TABLES
    finally:
        command.downgrade(config, "base")
        command.upgrade(config, "head")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("carrier", "error_pattern"),
    [
        (
            _carrier_parameters(
                ALPHA_ID,
                "carrier-alpha",
                "incompatible-alpha-adapter",
            ),
            "carrier-alpha.*adapter_key must be 'alpha'",
        ),
        (
            _carrier_parameters(
                ALPHA_ID,
                "different-code-on-alpha-id",
                "different-adapter",
            ),
            "deterministic UUID.*already assigned",
        ),
    ],
    ids=["incompatible-adapter", "deterministic-uuid-occupied"],
)
def test_0003_rejects_incompatible_reference_data_transactionally(
    monkeypatch: pytest.MonkeyPatch,
    carrier: dict[str, Any],
    error_pattern: str,
) -> None:
    database_url = os.environ.get("TEST_LEGACY_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_LEGACY_DATABASE_URL must point to a dedicated PostgreSQL 18 database")

    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    settings = _settings(database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0002_orders_shipments")
        setup_database = Database.from_settings(settings)
        try:

            async def insert_incompatible_carrier() -> None:
                async with setup_database.engine.begin() as connection:
                    await connection.execute(text(CARRIER_INSERT), carrier)

            run_async(insert_incompatible_carrier())
        finally:
            run_async(setup_database.dispose())

        with pytest.raises(RuntimeError, match=error_pattern):
            command.upgrade(config, "0003_carriers_tracking")

        rejected_database = Database.from_settings(settings)
        try:
            revision, tables = run_async(_revision_and_business_tables(rejected_database))

            async def carrier_rows() -> list[tuple[UUID, str, str]]:
                async with rejected_database.engine.connect() as connection:
                    return list(
                        (
                            await connection.execute(
                                text("SELECT id, code, adapter_key FROM carriers")
                            )
                        )
                        .tuples()
                        .all()
                    )

            persisted_carriers = run_async(carrier_rows())
        finally:
            run_async(rejected_database.dispose())
        assert revision == "0002_orders_shipments"
        assert tables == {"orders", "carriers", "shipments"}
        assert persisted_carriers == [(carrier["id"], carrier["code"], carrier["adapter_key"])]
    finally:
        command.downgrade(config, "base")
        command.upgrade(config, "head")

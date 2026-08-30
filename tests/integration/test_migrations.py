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

BUSINESS_TABLES = {"orders", "carriers", "shipments"}
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
ORDER_ID = UUID("00000000-0000-4000-8000-000000000201")
CARRIER_ID = UUID("00000000-0000-4000-8000-000000000202")
SHIPMENT_ID = UUID("00000000-0000-4000-8000-000000000203")

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


def _carrier_parameters(identifier: UUID, code: str, adapter_key: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "code": code,
        "name": "Migration Carrier",
        "adapter_key": adapter_key,
        "active": True,
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
                        "('orders', 'carriers', 'shipments')"
                    )
                )
            ).scalars()
        )
    return revision, tables


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
                            "AND tablename IN ('orders', 'shipments')"
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
                            "'shipments'::regclass)"
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
                            "'fk_shipments_carrier_id_carriers')"
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
                            "('shipments', 'status_occurred_at'))"
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
        }.issubset(indexes)
        assert {
            "ck_orders_order_status",
            "ck_shipments_shipment_status",
            "ck_shipments_tracking_code_normalized",
            "uq_orders_external_reference",
            "uq_shipments_carrier_tracking_code",
        }.issubset(constraints)
        assert delete_actions == {
            "fk_shipments_carrier_id_carriers": "r",
            "fk_shipments_order_id_orders": "r",
        }
        assert column_types == {
            "orders.created_at": "timestamp with time zone",
            "orders.id": "uuid",
            "orders.status": "character varying",
            "shipments.id": "uuid",
            "shipments.status_occurred_at": "timestamp with time zone",
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
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL must point to a dedicated PostgreSQL 18 database")

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
        run_async(_verify_head_schema_and_constraints(settings))
        run_async(_verify_application_readiness(settings))

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
    assert revision == "0002_orders_shipments"
    assert tables == BUSINESS_TABLES

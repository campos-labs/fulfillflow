"""Owner-specific migration graphs and PostgreSQL credential isolation."""

import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url


@pytest.mark.integration
@pytest.mark.parametrize(
    ("variable", "configuration", "revision", "tables", "foreign_database"),
    [
        (
            "TEST_DATABASE_URL",
            "alembic_core.ini",
            "1301_core",
            {"orders", "shipments", "carriers", "notifications", "tracking_event_receipts"},
            "fulfillflow_tracking",
        ),
        (
            "TEST_TRACKING_DATABASE_URL",
            "alembic_tracking.ini",
            "1203_tracking",
            {"carrier_event_inbox", "tracking_events"},
            "fulfillflow_core",
        ),
        (
            "TEST_NOTIFICATIONS_DATABASE_URL",
            "alembic_notifications.ini",
            "1301_notifications",
            {"notifications"},
            "fulfillflow_core",
        ),
    ],
)
def test_owner_schema_roundtrip_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    configuration: str,
    revision: str,
    tables: set[str],
    foreign_database: str,
) -> None:
    dsn = os.environ.get(variable)
    if dsn is None:
        pytest.skip(f"{variable} must identify an isolated PostgreSQL 18 test database")
    monkeypatch.setenv("DATABASE_URL", dsn)
    configuration_object = Config(configuration)
    url = make_url(dsn).set(drivername="postgresql")
    try:
        command.downgrade(configuration_object, "base")
        with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
            assert (
                connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename <> 'alembic_version'"
                ).fetchall()
                == []
            )
        command.upgrade(configuration_object, "head")
        command.current(configuration_object, check_heads=True)
        command.check(configuration_object)
        with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                revision,
            )
            assert {
                row[0]
                for row in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename <> 'alembic_version'"
                )
            } == tables | {"message_inbox", "message_quarantine", "message_rearm"} | (
                set() if variable == "TEST_NOTIFICATIONS_DATABASE_URL" else {"message_outbox"}
            )
            assert all(
                row[0] in tables | {"message_inbox"}
                for row in connection.execute(
                    "SELECT confrelid::regclass::text FROM pg_constraint "
                    "WHERE contype='f' AND connamespace='public'::regnamespace"
                )
            )
            assert connection.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                "WHERE rolname=current_user"
            ).fetchone() == (False, False, False)
            assert connection.execute(
                "SELECT has_database_privilege(current_user, %s, 'CONNECT')", (foreign_database,)
            ).fetchone() == (False,)
            for other in {
                "fulfillflow_core",
                "fulfillflow_tracking",
                "fulfillflow_notifications",
            } - {url.database}:
                assert connection.execute(
                    "SELECT has_database_privilege(current_user, %s, 'CONNECT')", (other,)
                ).fetchone() == (False,)
        with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
            psycopg.connect(
                url.set(database=foreign_database).render_as_string(hide_password=False)
            )
    finally:
        command.upgrade(configuration_object, "head")

"""Upgrade representative v1.1 records without publishing legacy work."""

import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url


@pytest.mark.parametrize(
    "owner,variable", [("core", "TEST_DATABASE_URL"), ("tracking", "TEST_TRACKING_DATABASE_URL")]
)
def test_v11_records_survive_transport_upgrade(monkeypatch, owner, variable):
    url = os.environ[variable]
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(f"alembic_{owner}.ini")
    dsn = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    command.upgrade(config, "head")
    command.downgrade(config, f"1101_{owner}")
    try:
        with psycopg.connect(dsn) as connection:
            if owner == "core":
                connection.execute(
                    """INSERT INTO tracking_event_receipts
                    (event_id,carrier_id,external_event_id,content_sha256,result,created_at)
                    VALUES ('00000000-0000-4000-8000-000000009999',
                    '00000000-0000-4000-8000-000000000100','v11-upgrade',%s,
                    '{"kind":"rejected"}', '2026-09-01T00:00:00Z')""",
                    ("a" * 64,),
                )
                query = (
                    "SELECT * FROM tracking_event_receipts WHERE external_event_id='v11-upgrade'"
                )
            else:
                connection.execute(
                    """INSERT INTO carrier_event_inbox
                    (id,carrier_id,external_event_id,payload_sha256,raw_body,received_at,status,request_id)
                    VALUES ('00000000-0000-4000-8000-000000009999',
                    '00000000-0000-4000-8000-000000000100','v11-upgrade',%s,%s,
                    '2026-09-01T00:00:00Z','RECEIVED','00000000-0000-4000-8000-000000009998')""",
                    ("a" * 64, b'{ "legacy": true }'),
                )
                query = "SELECT * FROM carrier_event_inbox WHERE external_event_id='v11-upgrade'"
            before = connection.execute(query).fetchall()
        command.upgrade(config, "head")
        command.current(config, check_heads=True)
        command.check(config)
        with psycopg.connect(dsn) as connection:
            assert connection.execute(query).fetchall() == before
            assert connection.execute("SELECT count(*) FROM message_outbox").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM message_inbox").fetchone() == (0,)
    finally:
        command.upgrade(config, "head")
        with psycopg.connect(dsn) as connection:
            table = "tracking_event_receipts" if owner == "core" else "carrier_event_inbox"
            connection.execute(f"DELETE FROM {table} WHERE external_event_id='v11-upgrade'")

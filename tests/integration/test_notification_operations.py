"""Owner CLI, bounded attempts and terminal preservation against real PostgreSQL."""

import asyncio
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from tests.integration.test_notifications_owned import admit, import_records, legacy_record, process
from tests.message_support import NOW, result_message
from tests.notification_support import notification_message
from tests.operations_support import command
from tests.support import FixedClock

from fulfillflow.contracts.messages import encode_message
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.operations import RearmError, diagnose, rearm
from fulfillflow.messaging.store import RetryableItemError, process_one, put_message
from fulfillflow.notifications.message_handler import apply_notification
from fulfillflow.notifications.message_tables import tables
from fulfillflow.notifications.operations import guard_rearm

pytestmark = pytest.mark.integration
MODULE = "fulfillflow.notifications.operations"


async def test_notification_retry_budget_rearm_cli_and_no_second_generation_race(
    postgres_notifications_database,
    postgres_notifications_settings,
):
    database = postgres_notifications_database
    message = notification_message()
    digest = hashlib.sha256(encode_message(message)).hexdigest()
    await admit(database)
    now = NOW

    async def fail(session, envelope):
        raise RetryableItemError("synthetic item problem")

    for attempt, delay in enumerate((1, 5, 15, 60, 60), 1):
        async with database.session() as session, session.begin():
            assert await process_one(session, tables.inbox, now, fail)
        async with database.session() as session:
            row = (await session.execute(select(tables.inbox))).mappings().one()
            assert row["attempts"] == attempt
            assert row["state"] == ("BLOCKED" if attempt == 5 else "RETRY_WAIT")
            assert row["next_attempt_at"] == now + timedelta(seconds=delay)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
        async with database.session() as session, session.begin():
            assert not await process_one(session, tables.inbox, now, fail)
        now += timedelta(seconds=delay)
    args = [
        "rearm",
        "--stage",
        "inbox",
        "--id",
        message.message_id,
        "--expected-hash",
        digest,
        "--reason",
        "Item cause corrected",
        "--expected-database",
        database.engine.url.database,
    ]
    bad = await command(postgres_notifications_settings, MODULE, *args[:-1], "wrong-owner-db")
    assert bad.returncode == 2 and "DATABASE_MISMATCH" in bad.stdout
    wrong_hash = list(args)
    wrong_hash[6] = "0" * 64
    bad = await command(postgres_notifications_settings, MODULE, *wrong_hash)
    assert bad.returncode == 2 and "HASH_MISMATCH" in bad.stdout
    first, second = await asyncio.gather(
        *[command(postgres_notifications_settings, MODULE, *args) for _ in range(2)]
    )
    assert sorted([first.returncode, second.returncode]) == [0, 2]
    accepted = json.loads((first if first.returncode == 0 else second).stdout)
    assert accepted["generation"] == 1 and accepted["service"] == "notifications"
    assert accepted["body_sha256"] == digest and accepted["reason"] == "Item cause corrected"
    report = await command(
        postgres_notifications_settings, MODULE, "diagnose", "--id", message.event_id
    )
    data = json.loads(report.stdout)
    assert "outbox" not in data and data["database"] == database.engine.url.database
    assert len(data["rearms"]) == 1 and data["rearms"][0]["previous_attempts"] == 5
    assert data["inbox_items"][0]["hash_matches_body"] is True
    assert message.payload.recipient not in report.stdout and '"body"' not in report.stdout
    async with database.session() as session, session.begin():
        assert await process_one(
            session,
            tables.inbox,
            now + timedelta(days=3650),
            lambda s, e: apply_notification(s, e, FixedClock(now)),
        )
    async with database.session() as session:
        assert await session.scalar(select(tables.inbox.c.state)) == "DONE"
        assert await session.scalar(select(tables.inbox.c.generation)) == 1


@pytest.mark.parametrize("origin", ["SIMULATED", "FAILED", "LEGACY"])
async def test_rearm_never_changes_terminal_even_if_technical_state_is_corrupt(
    postgres_notifications_database,
    postgres_notifications_settings,
    origin,
    monkeypatch,
):
    database = postgres_notifications_database
    if origin == "LEGACY":
        await import_records(database, legacy_record())
    elif origin == "FAILED":
        from fulfillflow.notifications import domain

        monkeypatch.delitem(domain._STATUS_CONTENT, "POSTED")
    await admit(database)
    assert await process(database)
    async with database.session() as session:
        before = await session.scalar(text("SELECT row_to_json(n)::text FROM notifications n"))
    message = notification_message()
    args = [
        "rearm",
        "--stage",
        "inbox",
        "--id",
        message.message_id,
        "--expected-hash",
        hashlib.sha256(encode_message(message)).hexdigest(),
        "--reason",
        "Must remain terminal",
        "--expected-database",
        database.engine.url.database,
    ]
    for state in ("DONE", "BLOCKED"):
        async with database.session() as session, session.begin():
            await session.execute(update(tables.inbox).values(state=state))
        result = await command(postgres_notifications_settings, MODULE, *args)
        assert result.returncode == 2 and "TERMINAL_NOTIFICATION" in result.stdout
    async with database.session() as session:
        assert (
            await session.scalar(text("SELECT row_to_json(n)::text FROM notifications n")) == before
        )
        assert await session.scalar(text("SELECT count(*) FROM message_rearm")) == 0
        if origin == "LEGACY":
            assert (
                await session.scalar(select(tables.quarantine.c.reason))
                == "LEGACY_EVENT_SUPPRESSED"
            )


async def test_notification_rearm_audit_rolls_back_with_state(postgres_notifications_database):
    database = postgres_notifications_database
    await admit(database)
    message = notification_message()
    async with database.session() as session, session.begin():
        await session.execute(update(tables.inbox).values(state="BLOCKED"))
    with pytest.raises(RuntimeError, match="after audit"):
        async with database.session() as session, session.begin():
            await guard_rearm(session, message.message_id)
            await rearm(
                session,
                tables,
                "inbox",
                message.message_id,
                hashlib.sha256(encode_message(message)).hexdigest(),
                "Corrected",
                NOW,
            )
            raise RuntimeError("after audit")
    async with database.session() as session:
        assert await session.scalar(select(tables.inbox.c.state)) == "BLOCKED"
        assert await session.scalar(select(tables.inbox.c.generation)) == 0
        assert await session.scalar(text("SELECT count(*) FROM message_rearm")) == 0


async def test_core_flow_diagnostic_and_rearm_filter(postgres_database, postgres_settings):
    fact, result = notification_message(), result_message()
    async with postgres_database.session() as session, session.begin():
        for message in (fact, result):
            await put_message(session, core_tables.outbox, message, NOW)
        await session.execute(update(core_tables.outbox).values(state="BLOCKED"))
    report = await command(
        postgres_settings,
        "fulfillflow.core.operations",
        "diagnose",
        "--flow",
        fact.type,
        "--id",
        fact.event_id,
    )
    assert report.returncode == 0, report.stdout
    data = json.loads(report.stdout)
    assert data["flow"] == fact.type
    assert data["outbox"][0]["count"] == 1
    assert {item["type"] for item in data["outbox_items"]} == {fact.type}
    async with postgres_database.session() as session, session.begin():
        with pytest.raises(RearmError, match="FLOW_MISMATCH"):
            await rearm(
                session,
                core_tables,
                "outbox",
                result.message_id,
                hashlib.sha256(encode_message(result)).hexdigest(),
                "Corrected",
                NOW,
                fact.type,
            )
    async with postgres_database.session() as session:
        all_flows = await diagnose(session, core_tables, NOW, None)
        assert all_flows["outbox"][0]["count"] == 2

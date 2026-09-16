"""Real owner databases: audited rearm, failure budgets and concurrent processors."""

import asyncio
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from tests.message_support import NOW, command_message, result_message

from fulfillflow.contracts.messages import encode_message
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.messaging.operations import RearmError, diagnose, rearm
from fulfillflow.messaging.store import (
    RetryableItemError,
    claim_publications,
    mark_sent,
    process_one,
    put_message,
    retry_publication,
)
from fulfillflow.tracking.message_tables import tables as tracking_tables


@pytest.fixture(params=["core", "tracking"])
def owner(request, postgres_database, postgres_tracking_database):
    return (
        (postgres_database, core_tables, command_message())
        if request.param == "core"
        else (postgres_tracking_database, tracking_tables, result_message())
    )


@pytest.mark.parametrize("stage", ["inbox", "outbox"])
async def test_exhaust_rearm_and_complete_without_identity_change(owner, stage):
    database, tables, message = owner
    table = getattr(tables, stage)
    body = encode_message(message)
    digest = hashlib.sha256(body).hexdigest()
    async with database.session() as session, session.begin():
        await put_message(session, table, message, NOW)

    async def fail(session, message):
        raise RetryableItemError("not logged")

    when = NOW
    for attempt, delay in enumerate((1, 5, 15, 60, 60), 1):
        async with database.session() as session, session.begin():
            if stage == "inbox":
                assert await process_one(session, table, when, fail)
            else:
                (item,) = await claim_publications(session, table, when)
                await retry_publication(session, table, item, when)
            row = (await session.execute(select(table))).mappings().one()
            assert row["attempts"] == attempt
            assert row["next_attempt_at"] == when + timedelta(seconds=delay)
        when += timedelta(seconds=delay)
    async with database.session() as session, session.begin():
        assert row["state"] == "BLOCKED"
        assert (
            await rearm(
                session, tables, stage, message.message_id, digest, "Resolved local cause", when
            )
            == 1
        )
        audit = (await session.execute(select(tables.rearm))).mappings().one()
        assert audit["previous_attempts"] == 5
        assert audit["body_sha256"] == digest
        assert audit["generation"] == 1
        assert audit["previous_reason"] is not None

    async def success(session, message):
        await session.execute(text("SELECT 1"))

    async with database.session() as session, session.begin():
        if stage == "inbox":
            assert await process_one(session, table, when, success)
        else:
            (item,) = await claim_publications(session, table, when)
            await mark_sent(session, table, item, when)
        after = (await session.execute(select(table))).mappings().one()
        assert after["body"] == body and after["message_id"] == message.message_id
        assert after["state"] in ("DONE", "SENT")
        with pytest.raises(RearmError, match="NOT_BLOCKED"):
            await rearm(session, tables, stage, message.message_id, digest, "Cannot repeat", when)


async def test_rearm_guards_rollback_and_concurrent_generation(owner):
    database, tables, message = owner
    digest = hashlib.sha256(encode_message(message)).hexdigest()
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)
        await session.execute(update(tables.inbox).values(state="BLOCKED", attempts=5))
        with pytest.raises(RearmError, match="HASH_MISMATCH"):
            await rearm(
                session, tables, "inbox", message.message_id, "f" * 64, "Wrong content", NOW
            )
        with pytest.raises(RearmError, match="INVALID_REASON"):
            await rearm(session, tables, "inbox", message.message_id, digest, "bad\nreason", NOW)
    with pytest.raises(RuntimeError):
        async with database.session() as session, session.begin():
            await rearm(session, tables, "inbox", message.message_id, digest, "Rolled back", NOW)
            raise RuntimeError("interruption before commit")

    async def recover():
        try:
            async with database.session() as session, session.begin():
                return await rearm(
                    session, tables, "inbox", message.message_id, digest, "Concurrent repair", NOW
                )
        except RearmError:
            return "NOT_BLOCKED"

    assert sorted(await asyncio.gather(recover(), recover()), key=str) == [1, "NOT_BLOCKED"]
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(tables.rearm)) == 1
        report = await diagnose(session, tables, NOW, message.message_id)
        assert report["inbox_items"][0]["state"] == "PENDING"
        assert "body" not in report["inbox_items"][0]
        assert len(report["rearms"]) == 1


async def test_concurrent_processors_skip_locked_and_apply_once(owner):
    database, tables, message = owner
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def application(session, item):
        calls.append(item.message_id)
        entered.set()
        await release.wait()

    async def first():
        async with database.session() as session, session.begin():
            return await process_one(session, tables.inbox, NOW, application)

    task = asyncio.create_task(first())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with database.session() as session, session.begin():
            assert not await process_one(session, tables.inbox, NOW, application)
    finally:
        release.set()
        assert await task
    assert calls == [message.message_id]


async def test_real_statement_timeout_retries_but_connection_loss_rolls_back(owner):
    database, tables, message = owner
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)

    async def timeout(session, item):
        await session.execute(text("SET LOCAL statement_timeout = 10"))
        await session.execute(text("SELECT pg_sleep(0.1)"))

    async with database.session() as session, session.begin():
        assert await process_one(session, tables.inbox, NOW, timeout)
        assert await session.scalar(select(tables.inbox.c.state)) == "RETRY_WAIT"

    async def disconnect(session, item):
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        async with database.session() as killer, killer.begin():
            await killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await session.execute(text("SELECT 1"))

    with pytest.raises(OperationalError):
        async with database.session() as session, session.begin():
            await process_one(session, tables.inbox, NOW + timedelta(seconds=1), disconnect)
    async with database.session() as session:
        assert await session.scalar(select(tables.inbox.c.attempts)) == 1
        assert await session.scalar(select(tables.inbox.c.state)) == "RETRY_WAIT"


async def test_expired_publisher_cannot_fail_new_lease(owner):
    database, tables, message = owner
    async with database.session() as session, session.begin():
        await put_message(session, tables.outbox, message, NOW)
        (old,) = await claim_publications(session, tables.outbox, NOW)
    later = NOW + timedelta(seconds=31)
    async with database.session() as session, session.begin():
        (current,) = await claim_publications(session, tables.outbox, later)
    async with database.session() as session, session.begin():
        await retry_publication(session, tables.outbox, old, later)
        row = (await session.execute(select(tables.outbox))).mappings().one()
        assert row["lease_token"] == current.token
        assert row["attempts"] == 0


async def test_local_cli_guards_database_and_audits_rearm(owner):
    import json
    import os
    import subprocess
    import sys

    database, tables, message = owner
    digest = hashlib.sha256(encode_message(message)).hexdigest()
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)
        await session.execute(update(tables.inbox).values(state="BLOCKED"))
    environment = dict(
        os.environ,
        APP_ENV="test",
        SERVICE_ROLE=tables.owner,
        DATABASE_URL=database.engine.url.render_as_string(hide_password=False),
        SESSION_SECRET="test-session",
        CARRIER_ALPHA_WEBHOOK_SECRET="test-alpha",
        CARRIER_BETA_WEBHOOK_SECRET="test-beta",
    )

    async def cli(*args):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", f"fulfillflow.{tables.owner}.operations", *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )

    args = (
        "rearm",
        "--stage",
        "inbox",
        "--id",
        str(message.message_id),
        "--expected-hash",
        digest,
        "--reason",
        "Dependency corrected",
        "--expected-database",
    )
    refused = await cli(*args, "historical-database")
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["error"] == "DATABASE_MISMATCH"
    accepted = await cli(*args, database.engine.url.database)
    assert accepted.returncode == 0, accepted.stdout
    assert json.loads(accepted.stdout)["generation"] == 1
    report = await cli("diagnose", "--id", str(message.event_id))
    assert report.returncode == 0, report.stdout
    data = json.loads(report.stdout)
    assert data["inbox_items"][0]["generation"] == 1
    assert len(data["rearms"]) == 1
    assert "body" not in data["inbox_items"][0]


async def test_real_deadlock_rolls_back_item_but_preserves_retry(owner):
    database, tables, message = owner
    second = command_message(2) if tables.owner == "core" else result_message(2)
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)
        await put_message(session, tables.inbox, second, NOW)
    barrier = asyncio.Barrier(2)

    async def application(session, envelope):
        key = 91001 if envelope.message_id == message.message_id else 91002
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        await barrier.wait()
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 182003 - key})

    async def process():
        async with database.session() as session, session.begin():
            return await process_one(session, tables.inbox, NOW, application)

    async with asyncio.timeout(10):
        assert await asyncio.gather(process(), process()) == [True, True]
    async with database.session() as session:
        rows = (await session.execute(select(tables.inbox.c.state, tables.inbox.c.attempts))).all()
        assert sorted(rows) == [("DONE", 1), ("RETRY_WAIT", 1)]


async def test_diagnostic_reports_corrupt_envelope_without_exposing_bytes(owner):
    database, tables, message = owner
    digest = hashlib.sha256(encode_message(message)).hexdigest()
    async with database.session() as session, session.begin():
        await put_message(session, tables.inbox, message, NOW)
        await session.execute(
            update(tables.inbox).values(state="BLOCKED", body=b"sensitive bad body")
        )
        report = await diagnose(session, tables, NOW, message.message_id)
        item = report["inbox_items"][0]
        assert item["envelope_error"] == "INVALID_STORED_ENVELOPE"
        assert item["hash_matches_body"] is False
        assert "sensitive bad body" not in str(report)
        with pytest.raises(RearmError, match="HASH_MISMATCH"):
            await rearm(
                session, tables, "inbox", message.message_id, digest, "Cannot repair bytes", NOW
            )

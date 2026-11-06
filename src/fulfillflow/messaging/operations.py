"""Owner-local SQL diagnostics and explicit, immutable-payload recovery."""

import hashlib
import re
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.messages import ResultEnvelope, decode_message
from fulfillflow.messaging.tables import InboxTables, MessageTables


class RearmError(ValueError):
    """A controlled precondition failed; no work was changed."""


async def rearm(
    session: AsyncSession,
    tables: MessageTables | InboxTables,
    stage: str,
    message_id: UUID,
    expected_hash: str,
    reason: str,
    now: datetime,
    flow: str | None = None,
) -> int:
    if stage not in ("inbox", "outbox") or (
        stage == "outbox" and not isinstance(tables, MessageTables)
    ):
        raise RearmError("INVALID_STAGE")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise RearmError("INVALID_HASH")
    if not 3 <= len(reason.strip()) <= 240 or not reason.isprintable():
        raise RearmError("INVALID_REASON")
    table = (
        tables.outbox if stage == "outbox" and isinstance(tables, MessageTables) else tables.inbox
    )
    row = (
        (
            await session.execute(
                select(table).where(table.c.message_id == message_id).with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RearmError("NOT_FOUND")
    if flow is not None and row["type"] != flow:
        raise RearmError("FLOW_MISMATCH")
    if row["state"] != "BLOCKED":
        raise RearmError("NOT_BLOCKED")
    if (
        row["body_sha256"] != expected_hash
        or hashlib.sha256(row["body"]).hexdigest() != expected_hash
    ):
        raise RearmError("HASH_MISMATCH")
    generation = int(row["generation"]) + 1
    await session.execute(
        tables.rearm.insert().values(
            id=uuid4(),
            stage=stage,
            message_id=message_id,
            generation=generation,
            body_sha256=expected_hash,
            previous_attempts=row["attempts"],
            previous_reason=row["reason"],
            reason=reason.strip(),
            created_at=now,
        )
    )
    await session.execute(
        update(table)
        .where(table.c.message_id == message_id)
        .values(
            state="PENDING",
            attempts=0,
            generation=generation,
            reason=None,
            next_attempt_at=now,
            lease_token=None,
            lease_until=None,
        )
    )
    return generation


def safe_columns(table: Table) -> list[Any]:
    return [column for column in table.c if column.name != "body"]


async def diagnose(
    session: AsyncSession,
    tables: MessageTables | InboxTables,
    now: datetime,
    identity: UUID | None,
    flow: str | None = None,
) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    matched_ids = {identity} if identity and flow is None else set()
    owned_stages = [("inbox", tables.inbox)]
    if isinstance(tables, MessageTables):
        owned_stages.insert(0, ("outbox", tables.outbox))
    for stage, table in owned_stages:
        rows = (
            (
                await session.execute(
                    select(
                        table.c.state,
                        func.count().label("count"),
                        func.min(table.c.created_at).label("oldest"),
                        func.max(table.c.last_attempt_at).label("last_attempt_at"),
                        func.max(table.c.finished_at).label("last_finished_at"),
                    )
                    .where(*([table.c.type == flow] if flow else []))
                    .group_by(table.c.state)
                )
            )
            .mappings()
            .all()
        )
        stages[stage] = [
            dict(row, oldest_age_seconds=max(0, (now - row["oldest"]).total_seconds()))
            for row in rows
        ]
        if identity is not None:
            rows = (
                (
                    await session.execute(
                        select(table).where(
                            *([table.c.type == flow] if flow else []),
                            or_(
                                table.c.message_id == identity,
                                table.c.event_id == identity,
                                table.c.correlation_id == identity,
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows:
                item = {key: value for key, value in row.items() if key != "body"}
                matched_ids.add(row["message_id"])
                item["hash_matches_body"] = (
                    hashlib.sha256(row["body"]).hexdigest() == row["body_sha256"]
                )
                try:
                    envelope = decode_message(row["body"])
                except ValueError:
                    item["envelope_error"] = "INVALID_STORED_ENVELOPE"
                else:
                    item["request_id"] = envelope.request_id
                    if isinstance(envelope, ResultEnvelope):
                        item["decision"] = envelope.payload.result.model_dump(mode="json")
                items.append(item)
            stages[f"{stage}_items"] = items
    stages["quarantine_scope"] = "owner-wide; invalid envelopes cannot be safely flow-filtered"
    stages["quarantine_count"] = await session.scalar(
        select(func.count()).select_from(tables.quarantine)
    )
    stages["quarantine_recent"] = [
        dict(row)
        for row in (
            await session.execute(
                select(*safe_columns(tables.quarantine))
                .order_by(tables.quarantine.c.created_at.desc())
                .limit(20)
            )
        ).mappings()
    ]
    if identity is not None:
        stages["quarantine_items"] = [
            dict(row)
            for row in (
                await session.execute(
                    select(*safe_columns(tables.quarantine)).where(
                        tables.quarantine.c.id == identity
                    )
                )
            ).mappings()
        ]
        # Reasons are operator-authored, local-only; never emitted to runtime logs.
        stages["rearms"] = [
            dict(row)
            for row in (
                await session.execute(
                    select(tables.rearm)
                    .where(tables.rearm.c.message_id.in_(matched_ids))
                    .order_by(tables.rearm.c.generation)
                )
            ).mappings()
        ]
    return stages

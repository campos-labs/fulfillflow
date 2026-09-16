"""SQL-only durable operations; the caller owns every transaction."""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, Table, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.messages import MessageEnvelope, decode_message, encode_message
from fulfillflow.messaging.tables import MessageTables


class MessageConflictError(ValueError):
    """A durable identity was reused with different content."""


class RetryableItemError(Exception):
    """A failure specific to one item, safe for bounded local retry."""


class BlockedItemError(Exception):
    """Contract or unexpected application failure requiring intervention."""


@dataclass(frozen=True)
class Publication:
    message_id: UUID
    token: UUID
    body: bytes
    attempts: int
    generation: int


async def put_message(
    session: AsyncSession, table: Table, message: MessageEnvelope, now: datetime
) -> None:
    body = encode_message(message)
    digest = hashlib.sha256(body).hexdigest()
    inserted = await session.scalar(
        insert(table)
        .values(
            message_id=message.message_id,
            type=message.type,
            event_id=message.event_id,
            correlation_id=message.correlation_id,
            body=body,
            body_sha256=digest,
            state="PENDING",
            attempts=0,
            generation=0,
            next_attempt_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(table.c.message_id)
    )
    if inserted is not None:
        return
    rows = (
        (
            await session.execute(
                select(table).where(
                    or_(
                        table.c.message_id == message.message_id,
                        and_(table.c.type == message.type, table.c.event_id == message.event_id),
                    )
                )
            )
        )
        .mappings()
        .all()
    )
    if len(rows) != 1 or rows[0]["body_sha256"] != digest:
        raise MessageConflictError("MESSAGE_IDENTITY_CONFLICT")


async def quarantine(
    session: AsyncSession, tables: MessageTables, body: bytes, reason: str, now: datetime
) -> None:
    await session.execute(
        insert(tables.quarantine).values(
            id=uuid4(),
            fingerprint=hashlib.sha256(body).hexdigest(),
            body=body[:65536],
            original_size=len(body),
            reason=reason,
            created_at=now,
        )
    )


async def claim_publications(
    session: AsyncSession, table: Table, now: datetime, *, limit: int = 20
) -> list[Publication]:
    rows = (
        (
            await session.execute(
                select(table)
                .where(
                    or_(
                        and_(table.c.state == "PENDING", table.c.next_attempt_at <= now),
                        and_(table.c.state == "LEASED", table.c.lease_until <= now),
                    )
                )
                .order_by(table.c.created_at, table.c.message_id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .mappings()
        .all()
    )
    publications = []
    for row in rows:
        token = uuid4()
        await session.execute(
            update(table)
            .where(table.c.message_id == row["message_id"])
            .values(
                state="LEASED",
                lease_token=token,
                lease_until=now + timedelta(seconds=30),
                last_attempt_at=now,
            )
        )
        publications.append(
            Publication(
                row["message_id"], token, row["body"], row["attempts"] + 1, row["generation"]
            )
        )
    return publications


async def mark_sent(session: AsyncSession, table: Table, item: Publication, now: datetime) -> bool:
    changed = await session.scalar(
        update(table)
        .where(
            table.c.message_id == item.message_id,
            table.c.state == "LEASED",
            table.c.lease_token == item.token,
            table.c.lease_until > now,
        )
        .values(
            state="SENT",
            attempts=item.attempts,
            finished_at=now,
            lease_token=None,
            lease_until=None,
            reason=None,
        )
        .returning(table.c.message_id)
    )

    return changed is not None


async def _item_failure(
    session: AsyncSession,
    table: Table,
    row: RowMapping,
    now: datetime,
    *,
    retryable: bool,
    category: str = "APPLICATION_CONFLICT",
) -> None:
    attempts = row["attempts"] + 1
    blocked = not retryable or attempts >= 5
    await session.execute(
        update(table)
        .where(table.c.message_id == row["message_id"])
        .values(
            attempts=attempts,
            state="BLOCKED" if blocked else "RETRY_WAIT",
            next_attempt_at=now + timedelta(seconds=(1, 5, 15, 60, 60)[attempts - 1]),
            reason="ITEM_RETRY_EXHAUSTED"
            if retryable and blocked
            else "ITEM_RETRY"
            if retryable
            else category,
            last_attempt_at=now,
        )
    )


async def process_one(
    session: AsyncSession,
    table: Table,
    now: datetime,
    apply: Callable[[AsyncSession, MessageEnvelope], Awaitable[None]],
) -> bool:
    """Lock survives savepoint rollback; connection failures abort the entire transaction."""
    row = (
        (
            await session.execute(
                select(table)
                .where(
                    table.c.state.in_(("PENDING", "RETRY_WAIT")),
                    table.c.next_attempt_at <= now,
                )
                .order_by(table.c.created_at, table.c.message_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return False
    envelope = None
    try:
        async with session.begin_nested():
            envelope = decode_message(row["body"])
            await apply(session, envelope)
    except RetryableItemError:
        await _item_failure(session, table, row, now, retryable=True)
    except DBAPIError as error:
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate in ("40001", "40P01", "55P03", "57014"):
            await _item_failure(session, table, row, now, retryable=True)
        elif error.connection_invalidated or isinstance(error, (OperationalError, InterfaceError)):
            raise
        else:
            await _item_failure(session, table, row, now, retryable=False)
    except (BlockedItemError, MessageConflictError):
        await _item_failure(session, table, row, now, retryable=False)
    except Exception:
        # Preserve a finite diagnostic for unexpected item failures; never log the body.
        await _item_failure(
            session, table, row, now, retryable=False, category="UNEXPECTED_ITEM_ERROR"
        )
    else:
        await session.execute(
            update(table)
            .where(table.c.message_id == row["message_id"])
            .values(
                state="DONE",
                attempts=row["attempts"] + 1,
                last_attempt_at=now,
                finished_at=now,
                reason=None,
            )
        )
    result = (
        (
            await session.execute(
                select(table.c.state, table.c.reason, table.c.attempts).where(
                    table.c.message_id == row["message_id"]
                )
            )
        )
        .mappings()
        .one()
    )
    session.info["message_activity"] = dict(
        row, **result, request_id=envelope.request_id if envelope is not None else None
    )
    return True


async def retry_publication(
    session: AsyncSession,
    table: Table,
    item: Publication,
    now: datetime,
    *,
    retryable: bool = True,
) -> str | None:
    """Returned/nacked publication has a finite durable retry budget."""
    row = (
        (
            await session.execute(
                select(table)
                .where(
                    table.c.message_id == item.message_id,
                    table.c.state == "LEASED",
                    table.c.lease_token == item.token,
                    table.c.lease_until > now,
                )
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    attempts = row["attempts"] + 1
    state = "BLOCKED" if not retryable or attempts >= 5 else "PENDING"
    await session.execute(
        update(table)
        .where(table.c.message_id == item.message_id)
        .values(
            attempts=attempts,
            state=state,
            next_attempt_at=now + timedelta(seconds=(1, 5, 15, 60, 60)[attempts - 1]),
            lease_token=None,
            lease_until=None,
            reason=(
                "UNEXPECTED_PUBLICATION_ERROR"
                if not retryable
                else "PUBLISH_RETRY_EXHAUSTED"
                if attempts >= 5
                else "PUBLISH_REJECTED"
            ),
            last_attempt_at=now,
        )
    )
    return state

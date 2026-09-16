"""Service-local transport tables; each owner supplies its own metadata."""

from dataclasses import dataclass

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)


@dataclass(frozen=True)
class MessageTables:
    outbox: Table
    inbox: Table
    quarantine: Table


def message_tables(metadata: MetaData) -> MessageTables:
    tables = []
    for name, states in (
        ("message_outbox", "'PENDING', 'LEASED', 'SENT', 'BLOCKED'"),
        ("message_inbox", "'PENDING', 'RETRY_WAIT', 'DONE', 'BLOCKED'"),
    ):
        table = Table(
            name,
            metadata,
            Column("message_id", Uuid, primary_key=True),
            Column("type", String(32), nullable=False),
            Column("event_id", Uuid, nullable=False),
            Column("correlation_id", Uuid, nullable=False),
            Column("body", LargeBinary, nullable=False),
            Column("body_sha256", CHAR(64), nullable=False),
            Column("state", String(16), nullable=False),
            Column("attempts", Integer, nullable=False),
            Column("generation", Integer, nullable=False),
            Column("next_attempt_at", DateTime(timezone=True), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("finished_at", DateTime(timezone=True)),
            Column("lease_token", Uuid),
            Column("lease_until", DateTime(timezone=True)),
            Column("reason", String(64)),
            UniqueConstraint("type", "event_id", name=f"uq_{name}_type_event"),
            CheckConstraint(f"state IN ({states})", name="state"),
            CheckConstraint("type IN ('tracking.apply.v1', 'tracking.result.v1')", name="type"),
            CheckConstraint("attempts BETWEEN 0 AND 5 AND generation >= 0", name="attempts"),
            CheckConstraint("octet_length(body) <= 65536", name="body_limit"),
            CheckConstraint("body_sha256 ~ '^[0-9a-f]{64}$'", name="body_hash"),
            CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease_pair"),
        )
        Index(f"ix_{name}_pending", table.c.state, table.c.next_attempt_at)
        tables.append(table)
    quarantine = Table(
        "message_quarantine",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("fingerprint", CHAR(64), nullable=False),
        Column("body", LargeBinary, nullable=False),
        Column("original_size", Integer, nullable=False),
        Column("reason", String(64), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("octet_length(body) <= 65536", name="body_limit"),
        CheckConstraint("original_size >= octet_length(body)", name="original_size"),
    )
    return MessageTables(tables[0], tables[1], quarantine)

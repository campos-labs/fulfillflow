"""Core owns these tables in its own database."""

from sqlalchemy import Index

from fulfillflow.db.base import Base
from fulfillflow.messaging.tables import message_tables

tables = message_tables(
    Base.metadata,
    "core",
    outbox_types=("tracking.apply.v1", "tracking.result.v1", "shipment.status_changed.v1"),
)
Index(
    "ix_message_outbox_flow_pending",
    tables.outbox.c.type,
    tables.outbox.c.state,
    tables.outbox.c.next_attempt_at,
)

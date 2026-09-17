"""Post-request assertions over two independent database observations."""

import os
from typing import Any
from uuid import UUID

from sqlalchemy import text

from fulfillflow.config import DatabaseSettings
from fulfillflow.db import Database


class EventState(dict[str, Any]):
    """Named observation fields; these reads do not promise a distributed snapshot."""

    def __getattr__(self, name: str) -> Any:
        return self[name]

    @property
    def _mapping(self) -> dict[str, Any]:
        return self


async def event_state(
    core: Database, tracking: Database, *, shipment_id: UUID | str, event_id: str
) -> EventState:
    async with tracking.engine.connect() as connection:
        inbox = (
            (
                await connection.execute(
                    text(
                        "SELECT i.*, i.id AS inbox_id, i.status AS inbox_status, "
                        "t.id AS tracking_event_id, t.application_result, "
                        "(SELECT count(*) FROM carrier_event_inbox) AS inbox_count, "
                        "(SELECT count(*) FROM tracking_events) AS tracking_count "
                        "FROM carrier_event_inbox i LEFT JOIN tracking_events t "
                        "ON t.inbox_event_id=i.id "
                        "WHERE i.external_event_id=:event_id"
                    ),
                    {"event_id": event_id},
                )
            )
            .mappings()
            .one()
        )
    state = EventState(inbox)
    async with core.engine.connect() as connection:
        shipment = (
            (
                await connection.execute(
                    text(
                        "SELECT s.status AS shipment_status, s.status_occurred_at, "
                        "s.status_event_received_at, s.status_external_event_id, "
                        "s.shipped_at, s.delivered_at, "
                        "s.updated_at AS shipment_updated_at, o.status AS order_status, "
                        "o.updated_at AS order_updated_at, "
                        "(SELECT count(*) FROM tracking_event_receipts) AS receipt_count "
                        "FROM shipments s JOIN orders o ON o.id=s.order_id WHERE s.id=:shipment_id"
                    ),
                    {"shipment_id": UUID(str(shipment_id))},
                )
            )
            .mappings()
            .one()
        )
        state.update(shipment)
    notifications = Database.from_settings(
        DatabaseSettings(_env_file=None, database_url=os.environ["TEST_NOTIFICATIONS_DATABASE_URL"])
    )
    try:
        async with notifications.engine.connect() as connection:
            state["notification_count"] = await connection.scalar(
                text("SELECT count(*) FROM notifications")
            )
            identifier = state["tracking_event_id"] or (state["command"] or {}).get("event_id")
            if identifier is not None:
                notification = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status AS notification_status, error_detail, "
                                "message, simulated_at "
                                "FROM notifications WHERE tracking_event_id=:id"
                            ),
                            {"id": UUID(str(identifier))},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if notification is not None:
                    state.update(notification)
    finally:
        await notifications.dispose()
    return state

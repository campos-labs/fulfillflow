"""One SQL-only simulation, participating in the processor's inbox transaction."""

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.messages import MessageEnvelope, NotificationEnvelope, encode_message
from fulfillflow.messaging.store import BlockedItemError, quarantine
from fulfillflow.notifications.domain import build_notification
from fulfillflow.notifications.message_tables import tables
from fulfillflow.notifications.owned_repository import OwnedNotificationRepository
from fulfillflow.shared import Clock, new_uuid


async def apply_notification(session: AsyncSession, message: MessageEnvelope, clock: Clock) -> None:
    if not isinstance(message, NotificationEnvelope):
        raise BlockedItemError("WRONG_MESSAGE_FLOW")
    if not session.in_transaction():
        raise RuntimeError("Notification processing requires an owning transaction")
    repository = OwnedNotificationRepository(session)
    existing = await repository.for_event(message.event_id)
    if existing is not None:
        if existing.origin == "LEGACY":
            # No historical envelope exists: retain the received evidence, never invent equality.
            await quarantine(
                session, tables, encode_message(message), "LEGACY_EVENT_SUPPRESSED", clock.now()
            )
        elif existing.message_id != message.message_id:
            raise BlockedItemError("NOTIFICATION_IDENTITY_CONFLICT")
        return
    notification = build_notification(
        notification_id=new_uuid(),
        shipment_id=message.payload.shipment_id,
        tracking_event_id=message.event_id,
        recipient=message.payload.recipient,
        resulting_status=message.payload.resulting_status,
        created_at=clock.now(),
    )
    await repository.add(notification, message.message_id)

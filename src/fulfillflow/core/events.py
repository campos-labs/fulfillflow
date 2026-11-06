"""Core business effects, composed by the atomic command/outbox handler."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.carriers.public import CarrierNotFoundError, CarriersPublic
from fulfillflow.contracts.core import (
    AppliedEventResult,
    ApplyEventCommand,
    EventResult,
    RejectedEventResult,
)
from fulfillflow.contracts.messages import NotificationPayload
from fulfillflow.contracts.values import ShipmentApplicationResult as ResultValue
from fulfillflow.contracts.values import ShipmentStatus as StatusValue
from fulfillflow.shared import Clock
from fulfillflow.shipments.public import (
    ShipmentApplicationResult,
    ShipmentReceipts,
    ShipmentsPublic,
    ShipmentStatus,
)


@dataclass(frozen=True)
class CoreApplication:
    result: EventResult
    notification: NotificationPayload | None = None


class CoreEventService:
    """Participate in the command handler's transaction without remote I/O."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._receipts = ShipmentReceipts(session)
        self._shipments = ShipmentsPublic(session)
        self._carriers = CarriersPublic(session)

    async def apply_in_transaction(self, command: ApplyEventCommand) -> CoreApplication:
        """Participate in the worker's inbox/effects/result-outbox transaction."""
        if not self._session.in_transaction():
            raise RuntimeError("Core event application requires an owning transaction")
        if not await self._carriers.views_by_ids({command.carrier_id}):
            raise CarrierNotFoundError(str(command.carrier_id))
        original = await self._receipts.claim(command)
        if original is not None:
            return CoreApplication(original)
        application = await self._apply_claimed(command)
        await self._receipts.finalize(command, application.result)
        return application

    async def _apply_claimed(self, command: ApplyEventCommand) -> CoreApplication:
        shipment = await self._shipments.lock_for_tracking(
            command.carrier_id, command.tracking_code
        )
        decided_at = self._clock.now()
        if shipment is None:
            return CoreApplication(
                RejectedEventResult(event_id=command.event_id, decided_at=decided_at)
            )
        transition = self._shipments.evaluate_tracking_status_locked(
            shipment,
            ShipmentStatus(command.canonical_status),
            occurred_at=command.occurred_at,
            received_at=command.received_at,
            external_event_id=command.external_event_id,
        )
        recipient = await self._shipments.persist_tracking_status_locked(shipment, transition)
        notification = None
        if transition.result is ShipmentApplicationResult.APPLIED:
            if recipient is None:
                raise RuntimeError("Applied Shipment transition has no notification recipient")
            notification = NotificationPayload(
                shipment_id=shipment.id,
                recipient=recipient.strip(),
                resulting_status=command.canonical_status,
            )
        await self._shipments.complete_order_if_eligible_locked(
            shipment, transition, occurred_at=command.received_at
        )
        return CoreApplication(
            AppliedEventResult(
                event_id=command.event_id,
                shipment_id=shipment.id,
                result=ResultValue(transition.result.value),
                previous_status=StatusValue(transition.previous_status.value),
                current_status=StatusValue(transition.resulting_status.value),
                decided_at=decided_at,
            ),
            notification,
        )

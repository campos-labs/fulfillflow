"""Database-authoritative Core idempotency; no independent transaction boundary."""

import hmac

from pydantic import TypeAdapter
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.core import ApplyEventCommand, EventResult
from fulfillflow.contracts.problems import EventIdentityConflictError
from fulfillflow.shipments.receipt_models import EventReceiptModel

_RESULT = TypeAdapter[EventResult](EventResult)


class ShipmentReceipts:
    """Participate in the Core coordinator's transaction through the public facade."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, command: ApplyEventCommand) -> EventResult | None:
        """Wait on the unique index before reading a concurrent original result."""
        content_hash = command.content_hash()
        inserted = await self._session.scalar(
            insert(EventReceiptModel)
            .values(
                event_id=command.event_id,
                carrier_id=command.carrier_id,
                external_event_id=command.external_event_id,
                content_sha256=content_hash,
                # Only an uncommitted reservation: any failure rolls it back with effects.
                result={},
                created_at=command.received_at,
            )
            .on_conflict_do_nothing()
            .returning(EventReceiptModel.event_id)
        )
        if inserted is not None:
            return None
        originals = (
            await self._session.scalars(
                select(EventReceiptModel).where(
                    or_(
                        EventReceiptModel.event_id == command.event_id,
                        and_(
                            EventReceiptModel.carrier_id == command.carrier_id,
                            EventReceiptModel.external_event_id == command.external_event_id,
                        ),
                    )
                )
            )
        ).all()
        if len(originals) != 1 or not hmac.compare_digest(
            originals[0].content_sha256, content_hash
        ):
            raise EventIdentityConflictError
        return _RESULT.validate_python(originals[0].result)

    async def finalize(self, command: ApplyEventCommand, result: EventResult) -> None:
        """Store the complete original decision before the local effects commit."""
        await self._session.execute(
            update(EventReceiptModel)
            .where(EventReceiptModel.event_id == command.event_id)
            .values(result=result.model_dump(mode="json"))
        )

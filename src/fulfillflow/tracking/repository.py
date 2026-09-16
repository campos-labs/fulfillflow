"""Tracking-owned persistence operations; this module never commits."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.shared import Page
from fulfillflow.tracking.domain import (
    CarrierEventInbox,
    InboxStatus,
    ShipmentApplicationResult,
    ShipmentStatus,
    TrackingEvent,
)
from fulfillflow.tracking.models import CarrierEventInboxModel, TrackingEventModel


@dataclass(frozen=True, slots=True)
class InboxFilters:
    """Resolved filters over fields owned by the Tracking inbox."""

    carrier_id: UUID | None = None
    status: InboxStatus | None = None
    external_event_id: str | None = None
    received_from: datetime | None = None
    received_to: datetime | None = None


class TrackingRepository:
    """Map Tracking domain entities to their module-owned records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_inbox(self, inbox: CarrierEventInbox) -> None:
        """Stage and flush one authenticated payload without committing."""
        self._session.add(_inbox_to_model(inbox))
        await self._session.flush()

    async def get_inbox_by_carrier_event(
        self,
        carrier_id: UUID,
        external_event_id: str,
    ) -> CarrierEventInbox | None:
        """Load the authoritative inbox row for an idempotency key."""
        statement = select(CarrierEventInboxModel).where(
            CarrierEventInboxModel.carrier_id == carrier_id,
            CarrierEventInboxModel.external_event_id == external_event_id,
        )
        model = await self._session.scalar(statement)
        return _inbox_to_entity(model) if model is not None else None

    async def get_inbox(
        self,
        inbox_event_id: UUID,
        *,
        for_update: bool = False,
    ) -> CarrierEventInbox | None:
        """Load one inbox record, optionally acquiring the first business lock."""
        statement = select(CarrierEventInboxModel).where(
            CarrierEventInboxModel.id == inbox_event_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        model = await self._session.scalar(statement)
        return _inbox_to_entity(model) if model is not None else None

    async def save_inbox(self, inbox: CarrierEventInbox) -> None:
        """Persist mutable processing fields and flush without committing."""
        model = await self._session.get(CarrierEventInboxModel, inbox.id)
        if model is None:
            raise LookupError(f"Carrier inbox {inbox.id} disappeared during its transaction")
        model.parsed_payload = inbox.parsed_payload
        model.command = inbox.command
        model.status = inbox.status.value
        model.error_code = inbox.error_code
        model.error_detail = inbox.error_detail
        model.processed_at = inbox.processed_at
        model.completed_at = inbox.completed_at
        model.result = inbox.result
        await self._session.flush()

    async def add_tracking_event(self, event: TrackingEvent) -> None:
        """Append and flush one normalized timeline event without committing."""
        self._session.add(_tracking_to_model(event))
        await self._session.flush()

    async def get_tracking_event_by_inbox(
        self,
        inbox_event_id: UUID,
    ) -> TrackingEvent | None:
        """Load the unique normalized result associated with an inbox row."""
        statement = select(TrackingEventModel).where(
            TrackingEventModel.inbox_event_id == inbox_event_id
        )
        model = await self._session.scalar(statement)
        return _tracking_to_entity(model) if model is not None else None

    async def timeline(
        self,
        shipment_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> Page[TrackingEvent]:
        """Return one stable newest-first page of a Shipment timeline."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(TrackingEventModel)
            .where(TrackingEventModel.shipment_id == shipment_id)
        )
        statement = (
            select(TrackingEventModel)
            .where(TrackingEventModel.shipment_id == shipment_id)
            .order_by(
                TrackingEventModel.occurred_at.desc(),
                TrackingEventModel.created_at.desc(),
                TrackingEventModel.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        models = (await self._session.scalars(statement)).all()
        return Page(
            items=[_tracking_to_entity(model) for model in models],
            page=page,
            page_size=page_size,
            total=total or 0,
        )

    async def list_inbox(
        self,
        filters: InboxFilters,
        *,
        page: int,
        page_size: int,
    ) -> Page[CarrierEventInbox]:
        """Return a stable newest-first operational inbox page."""
        predicates = []
        if filters.carrier_id is not None:
            predicates.append(CarrierEventInboxModel.carrier_id == filters.carrier_id)
        if filters.status is not None:
            predicates.append(CarrierEventInboxModel.status == filters.status.value)
        if filters.external_event_id is not None:
            predicates.append(CarrierEventInboxModel.external_event_id == filters.external_event_id)
        if filters.received_from is not None:
            predicates.append(CarrierEventInboxModel.received_at >= filters.received_from)
        if filters.received_to is not None:
            predicates.append(CarrierEventInboxModel.received_at <= filters.received_to)

        total = await self._session.scalar(
            select(func.count()).select_from(CarrierEventInboxModel).where(*predicates)
        )
        statement = (
            select(CarrierEventInboxModel)
            .where(*predicates)
            .order_by(
                CarrierEventInboxModel.received_at.desc(),
                CarrierEventInboxModel.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        models = (await self._session.scalars(statement)).all()
        return Page(
            items=[_inbox_to_entity(model) for model in models],
            page=page,
            page_size=page_size,
            total=total or 0,
        )


def _inbox_to_model(inbox: CarrierEventInbox) -> CarrierEventInboxModel:
    return CarrierEventInboxModel(
        id=inbox.id,
        carrier_id=inbox.carrier_id,
        external_event_id=inbox.external_event_id,
        payload_sha256=inbox.payload_sha256,
        raw_body=inbox.raw_body,
        parsed_payload=inbox.parsed_payload,
        received_at=inbox.received_at,
        status=inbox.status.value,
        error_code=inbox.error_code,
        error_detail=inbox.error_detail,
        processed_at=inbox.processed_at,
        request_id=inbox.request_id,
        command=inbox.command,
        completed_at=inbox.completed_at,
        result=inbox.result,
    )


def _inbox_to_entity(model: CarrierEventInboxModel) -> CarrierEventInbox:
    return CarrierEventInbox(
        id=model.id,
        carrier_id=model.carrier_id,
        external_event_id=model.external_event_id,
        payload_sha256=model.payload_sha256,
        raw_body=model.raw_body,
        parsed_payload=model.parsed_payload,
        received_at=model.received_at,
        status=InboxStatus(model.status),
        error_code=model.error_code,
        error_detail=model.error_detail,
        processed_at=model.processed_at,
        request_id=model.request_id,
        command=model.command,
        completed_at=model.completed_at,
        result=model.result,
    )


def _tracking_to_model(event: TrackingEvent) -> TrackingEventModel:
    return TrackingEventModel(
        id=event.id,
        inbox_event_id=event.inbox_event_id,
        shipment_id=event.shipment_id,
        carrier_id=event.carrier_id,
        external_status=event.external_status,
        canonical_status=event.canonical_status.value,
        description=event.description,
        location=event.location,
        occurred_at=event.occurred_at,
        received_at=event.received_at,
        application_result=event.application_result.value,
        previous_shipment_status=(
            event.previous_shipment_status.value
            if event.previous_shipment_status is not None
            else None
        ),
        resulting_shipment_status=event.resulting_shipment_status.value,
        created_at=event.created_at,
    )


def _tracking_to_entity(model: TrackingEventModel) -> TrackingEvent:
    return TrackingEvent(
        id=model.id,
        inbox_event_id=model.inbox_event_id,
        shipment_id=model.shipment_id,
        carrier_id=model.carrier_id,
        external_status=model.external_status,
        canonical_status=ShipmentStatus(model.canonical_status),
        description=model.description,
        location=model.location,
        occurred_at=model.occurred_at,
        received_at=model.received_at,
        application_result=ShipmentApplicationResult(model.application_result),
        previous_shipment_status=(
            ShipmentStatus(model.previous_shipment_status)
            if model.previous_shipment_status is not None
            else None
        ),
        resulting_shipment_status=ShipmentStatus(model.resulting_shipment_status),
        created_at=model.created_at,
    )

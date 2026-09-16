"""Durable webhook admission and local operational queries."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, cast
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.core import (
    ApplyEventCommand,
    CarrierRead,
)
from fulfillflow.contracts.messages import CommandEnvelope
from fulfillflow.contracts.tracking import CarrierPayloadProjection, Progress, WebhookAccepted
from fulfillflow.messaging.store import put_message
from fulfillflow.shared import Clock, Page, new_uuid
from fulfillflow.tracking.adapters import (
    UnknownExternalStatusError,
    normalize_carrier_event,
    project_known_carrier_payload,
)
from fulfillflow.tracking.authentication import (
    InvalidWebhookSignatureError,
    StaleWebhookTimestampError,
    WebhookAuthentication,
    authenticate_webhook,
)
from fulfillflow.tracking.core_client import CoreClient
from fulfillflow.tracking.domain import (
    CarrierEventInbox,
    InboxStatus,
    JsonValue,
    ShipmentApplicationResult,
    ShipmentStatus,
    TrackingEvent,
)
from fulfillflow.tracking.errors import TrackingProblemError
from fulfillflow.tracking.message_tables import tables
from fulfillflow.tracking.repository import InboxFilters, TrackingRepository
from fulfillflow.tracking.schemas import CarrierEventFilters, WebhookResult

_EVENT_NAMESPACE = UUID("e56e170b-727a-5aa0-a172-6d6c8f176728")


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    """Application result projected by the webhook response schema."""

    external_event_id: str
    inbox_event_id: UUID
    tracking_event_id: UUID
    result: WebhookResult
    original_result: ShipmentApplicationResult | None
    shipment_id: UUID
    previous_status: ShipmentStatus | None
    current_status: ShipmentStatus
    request_id: UUID


@dataclass(frozen=True, slots=True)
class CarrierEventView:
    """Inbox read model with public Carrier and timeline identifiers."""

    inbox: CarrierEventInbox
    carrier_code: str
    tracking_event_id: UUID | None
    payload: CarrierPayloadProjection | None
    progress: Progress | None = None


class TrackingService:
    """Own local inbox transactions; release all SQL resources before HTTP."""

    def __init__(self, session: AsyncSession, clock: Clock, core: CoreClient) -> None:
        self._session = session
        self._clock = clock
        self._repository = TrackingRepository(session)
        self._core = core

    async def authenticate_and_process(
        self,
        carrier_code: str,
        authentication: WebhookAuthentication,
        raw_body: bytes,
        *,
        secret: str,
        tolerance_seconds: int,
        request_id: UUID,
    ) -> WebhookOutcome | WebhookAccepted:
        received_at = self._clock.now()
        try:
            authenticate_webhook(
                authentication,
                secret=secret,
                raw_body=raw_body,
                now=received_at,
                tolerance_seconds=tolerance_seconds,
            )
        except InvalidWebhookSignatureError as exc:
            raise TrackingProblemError(
                status_code=401,
                code="INVALID_WEBHOOK_SIGNATURE",
                title="Invalid webhook signature",
                detail="Webhook authentication failed.",
            ) from exc
        except StaleWebhookTimestampError as exc:
            raise TrackingProblemError(
                status_code=401,
                code="STALE_WEBHOOK_TIMESTAMP",
                title="Stale webhook timestamp",
                detail="Webhook timestamp is outside the accepted window.",
            ) from exc
        carrier = await self._core.find_carrier(carrier_code.strip().lower(), active_only=True)
        if carrier is None:
            raise RuntimeError("Active Carrier lookup returned no registry record")
        inbox = await self._receive_authenticated(
            carrier,
            authentication.event_id,
            raw_body,
            received_at=received_at,
            request_id=request_id,
        )
        if inbox.status is InboxStatus.REJECTED:
            raise _recorded_rejection(inbox)
        if inbox.status is InboxStatus.PROCESSED:
            async with self._session.begin():
                return await self._duplicate_outcome(inbox, request_id)
        return WebhookAccepted(
            inbox_event_id=inbox.id,
            external_event_id=inbox.external_event_id,
            received_at=inbox.received_at,
            request_id=request_id,
        )

    async def timeline(
        self, shipment_id: UUID, *, page: int, page_size: int
    ) -> Page[TrackingEvent]:
        await self._core.require_shipment(shipment_id)
        async with self._session.begin():
            return await self._repository.timeline(shipment_id, page=page, page_size=page_size)

    async def list_inbox(
        self, filters: CarrierEventFilters, *, page: int, page_size: int
    ) -> Page[CarrierEventView]:
        carrier_id = None
        if filters.carrier_code is not None:
            carrier = await self._core.find_carrier(filters.carrier_code.strip().lower())
            if carrier is None:
                return Page([], page, page_size, 0)
            carrier_id = carrier.id
        async with self._session.begin():
            result = await self._repository.list_inbox(
                InboxFilters(
                    carrier_id=carrier_id,
                    status=filters.status,
                    external_event_id=filters.external_event_id,
                    received_from=filters.received_from,
                    received_to=filters.received_to,
                ),
                page=page,
                page_size=page_size,
            )
            event_ids = {}
            progresses = {}
            for inbox in result.items:
                event = await self._repository.get_tracking_event_by_inbox(inbox.id)
                event_ids[inbox.id] = event.id if event is not None else None
                progresses[inbox.id] = await self._progress(inbox)
        carriers = await self._core.carriers_by_ids({item.carrier_id for item in result.items})
        return Page(
            [
                CarrierEventView(
                    item,
                    carriers[item.carrier_id].code,
                    event_ids[item.id],
                    None,
                    progresses[item.id],
                )
                for item in result.items
            ],
            result.page,
            result.page_size,
            result.total,
        )

    async def get_inbox(self, inbox_event_id: UUID) -> CarrierEventView:
        async with self._session.begin():
            inbox = await self._repository.get_inbox(inbox_event_id)
            if inbox is None:
                raise CarrierEventNotFoundError(inbox_event_id)
            event = await self._repository.get_tracking_event_by_inbox(inbox.id)
            progress = await self._progress(inbox)
        carriers = await self._core.carriers_by_ids({inbox.carrier_id})
        carrier = carriers[inbox.carrier_id]
        return CarrierEventView(
            inbox,
            carrier.code,
            event.id if event is not None else None,
            project_known_carrier_payload(carrier.adapter_key, inbox.parsed_payload)
            if inbox.parsed_payload is not None
            else None,
            progress,
        )

    async def _receive_authenticated(
        self,
        carrier: CarrierRead,
        external_event_id: str,
        raw_body: bytes,
        *,
        received_at: datetime,
        request_id: UUID,
    ) -> CarrierEventInbox:
        """Commit raw reception, normalization and command outbox atomically."""
        payload_sha256 = hashlib.sha256(raw_body).hexdigest()
        try:
            async with self._session.begin():
                inbox = CarrierEventInbox(
                    id=new_uuid(),
                    carrier_id=carrier.id,
                    external_event_id=external_event_id,
                    payload_sha256=payload_sha256,
                    raw_body=raw_body,
                    parsed_payload=None,
                    received_at=received_at,
                    status=InboxStatus.RECEIVED,
                    error_code=None,
                    error_detail=None,
                    processed_at=None,
                    request_id=request_id,
                )
                await self._repository.add_inbox(inbox)
                await self._admit(carrier, inbox)
            return inbox
        except IntegrityError as integrity_error:
            async with self._session.begin():
                original = await self._repository.get_inbox_by_carrier_event(
                    carrier.id, external_event_id
                )
                if original is None:
                    raise
                if not hmac.compare_digest(original.payload_sha256, payload_sha256):
                    raise TrackingProblemError(
                        status_code=409,
                        code="EVENT_ID_PAYLOAD_CONFLICT",
                        title="Carrier event payload conflict",
                        detail="The Carrier event ID was already used with different bytes.",
                    ) from integrity_error
                locked = await self._repository.get_inbox(original.id, for_update=True)
                if locked is None:
                    raise RuntimeError("Carrier inbox disappeared") from integrity_error
                if locked.status is InboxStatus.RECEIVED:
                    await self._admit(carrier, locked)
                return locked

    async def _admit(self, carrier: CarrierRead, inbox: CarrierEventInbox) -> None:
        existing = await self._session.scalar(
            select(tables.outbox.c.message_id).where(tables.outbox.c.correlation_id == inbox.id)
        )
        if existing is not None:
            return
        try:
            if inbox.command is None:
                inbox.parsed_payload = _decode_json(inbox.raw_body)
                command = _normalize_command(carrier, inbox)
                inbox.command = command.model_dump(mode="json")
            else:
                command = ApplyEventCommand.model_validate(inbox.command)
        except TrackingProblemError as error:
            inbox.mark_rejected(
                code=error.code,
                detail=error.detail,
                processed_at=self._clock.now(),
                parsed_payload=inbox.parsed_payload,
            )
            inbox.completed_at = self._clock.now()
            await self._repository.save_inbox(inbox)
            return
        message = CommandEnvelope(
            message_id=new_uuid(),
            event_id=command.event_id,
            correlation_id=inbox.id,
            causation_id=inbox.id,
            request_id=inbox.request_id,
            created_at=self._clock.now(),
            payload=command,
            payload_sha256=command.content_hash(),
        )
        await self._repository.save_inbox(inbox)
        await put_message(self._session, tables.outbox, message, self._clock.now())

    async def _progress(self, inbox: CarrierEventInbox) -> Progress | None:
        if inbox.completed_at is not None:
            return "COMPLETED"
        outgoing = await self._session.scalar(
            select(tables.outbox.c.state).where(tables.outbox.c.correlation_id == inbox.id)
        )
        if outgoing is None:
            return None
        incoming = await self._session.scalar(
            select(tables.inbox.c.state).where(
                tables.inbox.c.correlation_id == inbox.id, tables.inbox.c.state == "BLOCKED"
            )
        )
        if outgoing == "BLOCKED" or incoming == "BLOCKED":
            return "BLOCKED_LOCAL"
        return "AWAITING_RESULT" if outgoing == "SENT" else "QUEUED"

    async def _duplicate_outcome(
        self, inbox: CarrierEventInbox, request_id: UUID
    ) -> WebhookOutcome:
        event = await self._repository.get_tracking_event_by_inbox(inbox.id)
        if event is None:
            raise RuntimeError("Processed Carrier inbox has no TrackingEvent")
        return _outcome_from_event(
            inbox,
            event,
            result=WebhookResult.DUPLICATE,
            original_result=event.application_result,
            request_id=request_id,
        )


def _normalize_command(carrier: CarrierRead, inbox: CarrierEventInbox) -> ApplyEventCommand:
    try:
        canonical = normalize_carrier_event(carrier.adapter_key, inbox.parsed_payload)
    except UnknownExternalStatusError as exc:
        raise _permanent_problem(
            "UNKNOWN_EXTERNAL_STATUS",
            "Unknown external status",
            "The Carrier payload contains an unknown status.",
        ) from exc
    except ValidationError as exc:
        raise _permanent_problem(
            "VALIDATION_ERROR",
            "Invalid Carrier payload",
            "The Carrier payload does not match its external schema.",
        ) from exc
    if canonical.external_event_id != inbox.external_event_id:
        raise _permanent_problem(
            "EVENT_ID_MISMATCH",
            "Carrier event ID mismatch",
            "The signed event ID does not match the payload event ID.",
        )
    return ApplyEventCommand.model_validate(
        dict(
            canonical.model_dump(),
            event_id=uuid5(_EVENT_NAMESPACE, str(inbox.id)),
            carrier_id=inbox.carrier_id,
            payload_sha256=inbox.payload_sha256,
            received_at=inbox.received_at,
        )
    )


def _recorded_rejection_from_error(error: TrackingProblemError) -> TrackingProblemError:
    return TrackingProblemError(
        status_code=error.status_code, code=error.code, title=error.title, detail=error.detail
    )


class CarrierEventNotFoundError(LookupError):
    """Raised when an operational inbox identifier does not exist."""

    def __init__(self, inbox_event_id: UUID) -> None:
        self.inbox_event_id = inbox_event_id
        super().__init__(f"Carrier event {inbox_event_id} was not found.")


def _decode_json(raw_body: bytes) -> JsonValue:
    try:
        text = raw_body.decode("utf-8")
        parsed = json.loads(text, parse_constant=_invalid_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _permanent_problem(
            "INVALID_JSON",
            "Invalid JSON",
            "The authenticated webhook body is not valid UTF-8 JSON.",
        ) from exc
    return cast(JsonValue, parsed)


def _invalid_json_constant(value: str) -> NoReturn:
    del value
    raise ValueError("non-standard JSON constant")


def _permanent_problem(code: str, title: str, detail: str) -> TrackingProblemError:
    return TrackingProblemError(
        status_code=422,
        code=code,
        title=title,
        detail=detail,
        reject_inbox=True,
    )


def _recorded_rejection(inbox: CarrierEventInbox) -> TrackingProblemError:
    code = inbox.error_code or "VALIDATION_ERROR"
    return TrackingProblemError(
        status_code=422,
        code=code,
        title=_REJECTION_TITLES.get(code, "Carrier event rejected"),
        detail=inbox.error_detail or "The Carrier event was previously rejected.",
    )


def _outcome_from_event(
    inbox: CarrierEventInbox,
    event: TrackingEvent,
    *,
    result: WebhookResult,
    original_result: ShipmentApplicationResult | None,
    request_id: UUID,
) -> WebhookOutcome:
    return WebhookOutcome(
        external_event_id=inbox.external_event_id,
        inbox_event_id=inbox.id,
        tracking_event_id=event.id,
        result=result,
        original_result=original_result,
        shipment_id=event.shipment_id,
        previous_status=event.previous_shipment_status,
        current_status=event.resulting_shipment_status,
        request_id=request_id,
    )


_REJECTION_TITLES = {
    "INVALID_JSON": "Invalid JSON",
    "VALIDATION_ERROR": "Invalid Carrier payload",
    "EVENT_ID_MISMATCH": "Carrier event ID mismatch",
    "UNKNOWN_EXTERNAL_STATUS": "Unknown external status",
    "SHIPMENT_NOT_FOUND_FOR_TRACKING": "Shipment not found for tracking",
}

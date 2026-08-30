"""Synchronous authenticated Carrier-event processing coordinator."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.carriers.public import (
    CarrierPayloadProjection,
    CarriersPublic,
    CarrierView,
    UnknownExternalStatusError,
    normalize_carrier_event,
    project_known_carrier_payload,
)
from fulfillflow.shared import Clock, Page, new_uuid
from fulfillflow.shipments.public import (
    ShipmentApplicationResult as PublicShipmentApplicationResult,
)
from fulfillflow.shipments.public import ShipmentNotFoundError, ShipmentsPublic
from fulfillflow.shipments.public import ShipmentStatus as PublicShipmentStatus
from fulfillflow.tracking.authentication import (
    InvalidWebhookSignatureError,
    StaleWebhookTimestampError,
    WebhookAuthentication,
    authenticate_webhook,
)
from fulfillflow.tracking.domain import (
    CarrierEventInbox,
    InboxStatus,
    JsonValue,
    ShipmentApplicationResult,
    ShipmentStatus,
    TrackingEvent,
)
from fulfillflow.tracking.repository import InboxFilters, TrackingRepository
from fulfillflow.tracking.schemas import (
    CarrierEventFilters,
    WebhookResult,
)


class TrackingProblemError(Exception):
    """Known sanitized error translated to an RFC 9457 response."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        reject_inbox: bool = False,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.reject_inbox = reject_inbox
        super().__init__(detail)


class PayloadTooLargeError(TrackingProblemError):
    """Raised before authentication when the request exceeds its byte limit."""

    def __init__(self) -> None:
        super().__init__(
            status_code=413,
            code="PAYLOAD_TOO_LARGE",
            title="Webhook payload too large",
            detail="The webhook body exceeds the configured byte limit.",
        )


class UnsupportedWebhookMediaTypeError(TrackingProblemError):
    """Raised before authentication for a non-JSON content type."""

    def __init__(self) -> None:
        super().__init__(
            status_code=415,
            code="UNSUPPORTED_MEDIA_TYPE",
            title="Unsupported media type",
            detail="Carrier webhooks require Content-Type application/json.",
        )


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


class TrackingService:
    """Own the independent reception and atomic business transactions."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._repository = TrackingRepository(session)
        self._carriers = CarriersPublic(session)
        self._shipments = ShipmentsPublic(session)

    async def authenticate_and_process(
        self,
        carrier_code: str,
        authentication: WebhookAuthentication,
        raw_body: bytes,
        *,
        secret: str,
        tolerance_seconds: int,
        request_id: UUID,
    ) -> WebhookOutcome:
        """Authenticate raw bytes, commit the inbox, then process synchronously."""
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

        carrier, inbox = await self._receive_authenticated(
            carrier_code.strip().lower(),
            authentication.event_id,
            raw_body,
            received_at=received_at,
            request_id=request_id,
        )
        return await self._process_inbox(carrier, inbox, request_id=request_id)

    async def timeline(
        self,
        shipment_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> Page[TrackingEvent]:
        """Return an existing Shipment's canonical timeline."""
        async with self._session.begin():
            if await self._shipments.find(shipment_id) is None:
                raise ShipmentNotFoundError(shipment_id)
            return await self._repository.timeline(
                shipment_id,
                page=page,
                page_size=page_size,
            )

    async def list_inbox(
        self,
        filters: CarrierEventFilters,
        *,
        page: int,
        page_size: int,
    ) -> Page[CarrierEventView]:
        """List sanitized authenticated inbox records."""
        async with self._session.begin():
            carrier_id: UUID | None = None
            if filters.carrier_code is not None:
                carrier = await self._carriers.find(filters.carrier_code.strip().lower())
                if carrier is None:
                    return Page([], page, page_size, 0)
                carrier_id = carrier.id
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
            return await self._inbox_page_to_views(result)

    async def get_inbox(self, inbox_event_id: UUID) -> CarrierEventView:
        """Return sanitized operational detail for one inbox row."""
        async with self._session.begin():
            inbox = await self._repository.get_inbox(inbox_event_id)
            if inbox is None:
                raise CarrierEventNotFoundError(inbox_event_id)
            carriers = await self._carriers.views_by_ids({inbox.carrier_id})
            carrier = carriers[inbox.carrier_id]
            event = await self._repository.get_tracking_event_by_inbox(inbox.id)
            return CarrierEventView(
                inbox,
                carrier.code,
                event.id if event is not None else None,
                (
                    project_known_carrier_payload(
                        carrier.adapter_key,
                        inbox.parsed_payload,
                    )
                    if inbox.parsed_payload is not None
                    else None
                ),
            )

    async def _receive_authenticated(
        self,
        carrier_code: str,
        external_event_id: str,
        raw_body: bytes,
        *,
        received_at: datetime,
        request_id: UUID,
    ) -> tuple[CarrierView, CarrierEventInbox]:
        """Transaction A: preserve authenticated bytes under DB idempotency."""
        payload_sha256 = hashlib.sha256(raw_body).hexdigest()
        carrier: CarrierView
        try:
            async with self._session.begin():
                carrier = await self._carriers.require_active(carrier_code)
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
            return carrier, inbox
        except IntegrityError as integrity_error:
            async with self._session.begin():
                carrier = await self._carriers.require_active(carrier_code)
                original = await self._repository.get_inbox_by_carrier_event(
                    carrier.id,
                    external_event_id,
                )
            if original is None:
                raise
            if not hmac.compare_digest(
                original.payload_sha256.encode("ascii"),
                payload_sha256.encode("ascii"),
            ):
                raise TrackingProblemError(
                    status_code=409,
                    code="EVENT_ID_PAYLOAD_CONFLICT",
                    title="Carrier event payload conflict",
                    detail="The Carrier event ID was already used with different bytes.",
                ) from integrity_error
            return carrier, original

    async def _process_inbox(
        self,
        carrier: CarrierView,
        inbox: CarrierEventInbox,
        *,
        request_id: UUID,
    ) -> WebhookOutcome:
        """Transaction B: normalize, apply Shipment state and finalize atomically."""
        parsed_payload: JsonValue = None
        try:
            async with self._session.begin():
                locked = await self._repository.get_inbox(inbox.id, for_update=True)
                if locked is None:
                    raise RuntimeError("Carrier inbox disappeared after authenticated reception")
                if locked.status is InboxStatus.PROCESSED:
                    return await self._duplicate_outcome(locked, request_id)
                if locked.status is InboxStatus.REJECTED:
                    raise _recorded_rejection(locked)

                parsed_payload = _decode_json(locked.raw_body)
                locked.parsed_payload = parsed_payload
                try:
                    canonical = normalize_carrier_event(carrier.adapter_key, parsed_payload)
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

                if canonical.external_event_id != locked.external_event_id:
                    raise _permanent_problem(
                        "EVENT_ID_MISMATCH",
                        "Carrier event ID mismatch",
                        "The signed event ID does not match the payload event ID.",
                    )

                shipment = await self._shipments.lock_for_tracking(
                    carrier.id,
                    canonical.tracking_code,
                )
                if shipment is None:
                    raise _permanent_problem(
                        "SHIPMENT_NOT_FOUND_FOR_TRACKING",
                        "Shipment not found for tracking",
                        "No Shipment matches the Carrier and tracking code.",
                    )

                applied = await self._shipments.apply_tracking_status_locked(
                    shipment,
                    PublicShipmentStatus(canonical.canonical_status.value),
                    occurred_at=canonical.occurred_at,
                    received_at=locked.received_at,
                    external_event_id=canonical.external_event_id,
                )
                event = TrackingEvent(
                    id=new_uuid(),
                    inbox_event_id=locked.id,
                    shipment_id=shipment.id,
                    carrier_id=carrier.id,
                    external_status=canonical.external_status,
                    canonical_status=ShipmentStatus(canonical.canonical_status.value),
                    description=canonical.description,
                    location=canonical.location,
                    occurred_at=canonical.occurred_at,
                    received_at=locked.received_at,
                    application_result=ShipmentApplicationResult(applied.transition.result.value),
                    previous_shipment_status=ShipmentStatus(
                        applied.transition.previous_status.value
                    ),
                    resulting_shipment_status=ShipmentStatus(
                        applied.transition.resulting_status.value
                    ),
                    created_at=self._clock.now(),
                )
                await self._repository.add_tracking_event(event)
                locked.mark_processed(self._clock.now())
                await self._repository.save_inbox(locked)
                return _outcome_from_event(
                    locked,
                    event,
                    result=WebhookResult(event.application_result.value),
                    original_result=None,
                    request_id=request_id,
                )
        except TrackingProblemError as exc:
            if exc.reject_inbox:
                await self._reject_inbox(
                    inbox.id,
                    parsed_payload=parsed_payload,
                    error=exc,
                )
            raise

    async def _reject_inbox(
        self,
        inbox_event_id: UUID,
        *,
        parsed_payload: JsonValue,
        error: TrackingProblemError,
    ) -> None:
        """Persist one permanent rejection after Transaction B rolls back."""
        async with self._session.begin():
            inbox = await self._repository.get_inbox(inbox_event_id, for_update=True)
            if inbox is None:
                raise RuntimeError("Carrier inbox disappeared before rejection finalization")
            if inbox.status is not InboxStatus.RECEIVED:
                return
            inbox.mark_rejected(
                code=error.code,
                detail=error.detail,
                processed_at=self._clock.now(),
                parsed_payload=parsed_payload,
            )
            await self._repository.save_inbox(inbox)

    async def _duplicate_outcome(
        self,
        inbox: CarrierEventInbox,
        request_id: UUID,
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

    async def _inbox_page_to_views(
        self,
        page: Page[CarrierEventInbox],
    ) -> Page[CarrierEventView]:
        codes = await self._carriers.codes_by_ids({item.carrier_id for item in page.items})
        views: list[CarrierEventView] = []
        for inbox in page.items:
            event = await self._repository.get_tracking_event_by_inbox(inbox.id)
            views.append(
                CarrierEventView(
                    inbox,
                    codes[inbox.carrier_id],
                    event.id if event is not None else None,
                    None,
                )
            )
        return Page(views, page.page, page.page_size, page.total)


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


def _assert_public_result_coverage() -> None:
    """Keep the two public persisted result enums intentionally synchronized."""
    assert {item.value for item in PublicShipmentApplicationResult} == {
        item.value for item in ShipmentApplicationResult
    }


_assert_public_result_coverage()

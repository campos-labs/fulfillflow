"""Framework-independent Shipment entity and state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class ShipmentStatus(StrEnum):
    """Canonical Shipment states persisted as varchar values."""

    PENDING = "PENDING"
    POSTED = "POSTED"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"
    RETURNED = "RETURNED"
    CANCELLED = "CANCELLED"


class ShipmentApplicationResult(StrEnum):
    """Outcome of applying one already-normalized carrier status."""

    APPLIED = "APPLIED"
    NO_STATE_CHANGE = "NO_STATE_CHANGE"
    IGNORED_STALE = "IGNORED_STALE"
    IGNORED_INVALID_TRANSITION = "IGNORED_INVALID_TRANSITION"


class InvalidShipmentTransitionError(ValueError):
    """Raised for a disallowed manual Shipment command."""

    def __init__(self, current: ShipmentStatus, target: ShipmentStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Transition {current.value} -> {target.value} is not allowed.")


_ALLOWED_TRANSITIONS: dict[ShipmentStatus, frozenset[ShipmentStatus]] = {
    ShipmentStatus.PENDING: frozenset(
        {
            ShipmentStatus.POSTED,
            ShipmentStatus.IN_TRANSIT,
            ShipmentStatus.OUT_FOR_DELIVERY,
            ShipmentStatus.DELIVERED,
            ShipmentStatus.EXCEPTION,
            ShipmentStatus.CANCELLED,
        }
    ),
    ShipmentStatus.POSTED: frozenset(
        {
            ShipmentStatus.IN_TRANSIT,
            ShipmentStatus.OUT_FOR_DELIVERY,
            ShipmentStatus.DELIVERED,
            ShipmentStatus.EXCEPTION,
            ShipmentStatus.RETURNED,
        }
    ),
    ShipmentStatus.IN_TRANSIT: frozenset(
        {
            ShipmentStatus.OUT_FOR_DELIVERY,
            ShipmentStatus.DELIVERED,
            ShipmentStatus.EXCEPTION,
            ShipmentStatus.RETURNED,
        }
    ),
    ShipmentStatus.OUT_FOR_DELIVERY: frozenset(
        {
            ShipmentStatus.IN_TRANSIT,
            ShipmentStatus.DELIVERED,
            ShipmentStatus.EXCEPTION,
            ShipmentStatus.RETURNED,
        }
    ),
    ShipmentStatus.EXCEPTION: frozenset(
        {
            ShipmentStatus.IN_TRANSIT,
            ShipmentStatus.OUT_FOR_DELIVERY,
            ShipmentStatus.DELIVERED,
            ShipmentStatus.RETURNED,
        }
    ),
    ShipmentStatus.DELIVERED: frozenset(),
    ShipmentStatus.RETURNED: frozenset(),
    ShipmentStatus.CANCELLED: frozenset(),
}

_SHIPPED_STATES = frozenset(
    {
        ShipmentStatus.POSTED,
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
        ShipmentStatus.RETURNED,
    }
)


@dataclass(frozen=True, slots=True)
class ShipmentTransition:
    """State-machine outcome needed by a future Tracking coordinator."""

    result: ShipmentApplicationResult
    previous_status: ShipmentStatus
    resulting_status: ShipmentStatus


@dataclass(slots=True)
class Shipment:
    """Shipment aggregate data plus complete ordering and transition rules."""

    id: UUID
    order_id: UUID
    carrier_id: UUID
    tracking_code: str
    status: ShipmentStatus
    status_occurred_at: datetime
    status_event_received_at: datetime | None
    status_external_event_id: str | None
    estimated_delivery_date: date | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def cancel(self, occurred_at: datetime) -> bool:
        """Cancel only a pending Shipment; repeated cancellation is idempotent."""
        if self.status is ShipmentStatus.CANCELLED:
            return False
        if self.status is not ShipmentStatus.PENDING:
            raise InvalidShipmentTransitionError(self.status, ShipmentStatus.CANCELLED)
        self.status = ShipmentStatus.CANCELLED
        self.status_occurred_at = occurred_at
        self.updated_at = occurred_at
        return True

    def apply_external_status(
        self,
        target: ShipmentStatus,
        *,
        occurred_at: datetime,
        received_at: datetime,
        external_event_id: str,
    ) -> ShipmentTransition:
        """Apply one canonical carrier status using deterministic event ordering."""
        if not external_event_id or not external_event_id.strip() or len(external_event_id) > 128:
            raise ValueError("external_event_id must contain between 1 and 128 characters")
        previous = self.status
        incoming_key = (occurred_at, received_at, external_event_id)
        current_key = self._external_ordering_key()

        if current_key is not None and incoming_key <= current_key:
            return ShipmentTransition(
                ShipmentApplicationResult.IGNORED_STALE,
                previous,
                previous,
            )

        if target is previous:
            self._advance_external_ordering(occurred_at, received_at, external_event_id)
            self.updated_at = received_at
            return ShipmentTransition(
                ShipmentApplicationResult.NO_STATE_CHANGE,
                previous,
                previous,
            )

        if target not in _ALLOWED_TRANSITIONS[previous]:
            return ShipmentTransition(
                ShipmentApplicationResult.IGNORED_INVALID_TRANSITION,
                previous,
                previous,
            )

        self.status = target
        self._advance_external_ordering(occurred_at, received_at, external_event_id)
        if self.shipped_at is None and target in _SHIPPED_STATES:
            self.shipped_at = occurred_at
        if target is ShipmentStatus.DELIVERED:
            self.delivered_at = occurred_at
        self.updated_at = received_at
        return ShipmentTransition(ShipmentApplicationResult.APPLIED, previous, target)

    def _external_ordering_key(self) -> tuple[datetime, datetime, str] | None:
        received_at = self.status_event_received_at
        event_id = self.status_external_event_id
        if received_at is None or event_id is None:
            return None
        return (self.status_occurred_at, received_at, event_id)

    def _advance_external_ordering(
        self,
        occurred_at: datetime,
        received_at: datetime,
        external_event_id: str,
    ) -> None:
        self.status_occurred_at = occurred_at
        self.status_event_received_at = received_at
        self.status_external_event_id = external_event_id

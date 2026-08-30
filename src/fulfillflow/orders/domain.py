"""Framework-independent Order entity and state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class OrderStatus(StrEnum):
    """Canonical Order states persisted as varchar values."""

    CREATED = "CREATED"
    CONFIRMED = "CONFIRMED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"


class InvalidOrderTransitionError(ValueError):
    """Raised when an Order command is not allowed from its current state."""

    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Transition {current.value} -> {target.value} is not allowed.")


@dataclass(slots=True)
class Order:
    """Order aggregate data plus its complete v1.0.0 transition rules."""

    id: UUID
    external_reference: str
    recipient_name: str
    recipient_email: str
    recipient_postal_code: str
    recipient_city: str
    recipient_state: str
    status: OrderStatus
    created_at: datetime
    updated_at: datetime

    def confirm(self, occurred_at: datetime) -> bool:
        """Confirm a newly created Order, returning whether state changed."""
        if self.status is OrderStatus.CONFIRMED:
            return False
        if self.status is not OrderStatus.CREATED:
            raise InvalidOrderTransitionError(self.status, OrderStatus.CONFIRMED)
        self.status = OrderStatus.CONFIRMED
        self.updated_at = occurred_at
        return True

    def cancel(self, occurred_at: datetime) -> bool:
        """Cancel only a newly created Order; repeated cancellation is idempotent."""
        if self.status is OrderStatus.CANCELLED:
            return False
        if self.status is not OrderStatus.CREATED:
            raise InvalidOrderTransitionError(self.status, OrderStatus.CANCELLED)
        self.status = OrderStatus.CANCELLED
        self.updated_at = occurred_at
        return True

    def fulfill(self, occurred_at: datetime) -> bool:
        """Complete a confirmed Order through the public idempotent operation."""
        if self.status is OrderStatus.FULFILLED:
            return False
        if self.status is not OrderStatus.CONFIRMED:
            raise InvalidOrderTransitionError(self.status, OrderStatus.FULFILLED)
        self.status = OrderStatus.FULFILLED
        self.updated_at = occurred_at
        return True

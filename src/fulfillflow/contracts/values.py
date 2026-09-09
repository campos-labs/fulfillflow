"""Wire values; state transition rules remain with their owning service."""

from enum import StrEnum


class InboxStatus(StrEnum):
    """Lifecycle of one authenticated Carrier webhook."""

    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"


class ShipmentStatus(StrEnum):
    """Shipment status values copied into the immutable Tracking timeline.

    Tracking owns this persisted representation. Conversion to and from the
    Shipments public enum happens in the application service boundary.
    """

    PENDING = "PENDING"
    POSTED = "POSTED"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"
    RETURNED = "RETURNED"
    CANCELLED = "CANCELLED"


class ShipmentApplicationResult(StrEnum):
    """Result persisted for application of one canonical event."""

    APPLIED = "APPLIED"
    NO_STATE_CHANGE = "NO_STATE_CHANGE"
    IGNORED_STALE = "IGNORED_STALE"
    IGNORED_INVALID_TRANSITION = "IGNORED_INVALID_TRANSITION"

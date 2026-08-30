"""Complete unit coverage for Shipment transitions and deterministic ordering."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fulfillflow.shipments.public import (
    InvalidShipmentTransitionError,
    Shipment,
    ShipmentApplicationResult,
    ShipmentStatus,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
RECEIVED = NOW + timedelta(minutes=1)

ALLOWED = {
    ShipmentStatus.PENDING: {
        ShipmentStatus.POSTED,
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
        ShipmentStatus.EXCEPTION,
        ShipmentStatus.CANCELLED,
    },
    ShipmentStatus.POSTED: {
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
        ShipmentStatus.EXCEPTION,
        ShipmentStatus.RETURNED,
    },
    ShipmentStatus.IN_TRANSIT: {
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
        ShipmentStatus.EXCEPTION,
        ShipmentStatus.RETURNED,
    },
    ShipmentStatus.OUT_FOR_DELIVERY: {
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.DELIVERED,
        ShipmentStatus.EXCEPTION,
        ShipmentStatus.RETURNED,
    },
    ShipmentStatus.EXCEPTION: {
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
        ShipmentStatus.RETURNED,
    },
    ShipmentStatus.DELIVERED: set(),
    ShipmentStatus.RETURNED: set(),
    ShipmentStatus.CANCELLED: set(),
}


def _shipment(status: ShipmentStatus = ShipmentStatus.PENDING) -> Shipment:
    has_event = status is not ShipmentStatus.PENDING
    return Shipment(
        id=UUID("00000000-0000-4000-8000-000000000010"),
        order_id=UUID("00000000-0000-4000-8000-000000000001"),
        carrier_id=UUID("00000000-0000-4000-8000-000000000002"),
        tracking_code="TRACK001",
        status=status,
        status_occurred_at=NOW,
        status_event_received_at=RECEIVED if has_event else None,
        status_external_event_id="evt-001" if has_event else None,
        estimated_delivery_date=None,
        shipped_at=(
            NOW if status not in {ShipmentStatus.PENDING, ShipmentStatus.EXCEPTION} else None
        ),
        delivered_at=NOW if status is ShipmentStatus.DELIVERED else None,
        created_at=NOW,
        updated_at=RECEIVED if has_event else NOW,
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in ALLOWED.items() for target in targets],
)
def test_every_documented_external_transition_is_applied(
    current: ShipmentStatus,
    target: ShipmentStatus,
) -> None:
    shipment = _shipment(current)
    occurred_at = NOW + timedelta(hours=1)
    received_at = occurred_at + timedelta(minutes=1)

    transition = shipment.apply_external_status(
        target,
        occurred_at=occurred_at,
        received_at=received_at,
        external_event_id="evt-002",
    )

    assert transition.result is ShipmentApplicationResult.APPLIED
    assert transition.previous_status is current
    assert transition.resulting_status is target
    assert shipment.status is target
    assert shipment.status_occurred_at == occurred_at
    assert shipment.status_event_received_at == received_at
    assert shipment.status_external_event_id == "evt-002"
    assert shipment.updated_at == received_at


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current, targets in ALLOWED.items()
        for target in ShipmentStatus
        if target is not current and target not in targets
    ],
)
def test_every_undocumented_external_transition_is_ignored(
    current: ShipmentStatus,
    target: ShipmentStatus,
) -> None:
    shipment = _shipment(current)
    before = replace(shipment)

    transition = shipment.apply_external_status(
        target,
        occurred_at=NOW + timedelta(hours=1),
        received_at=NOW + timedelta(hours=1, minutes=1),
        external_event_id="evt-002",
    )

    assert transition.result is ShipmentApplicationResult.IGNORED_INVALID_TRANSITION
    assert transition.previous_status is current
    assert transition.resulting_status is current
    assert shipment == before


def test_first_external_event_is_not_stale_when_it_predates_creation() -> None:
    shipment = _shipment()
    occurred_at = NOW - timedelta(days=1)

    transition = shipment.apply_external_status(
        ShipmentStatus.POSTED,
        occurred_at=occurred_at,
        received_at=RECEIVED,
        external_event_id="evt-first",
    )

    assert transition.result is ShipmentApplicationResult.APPLIED
    assert shipment.shipped_at == occurred_at


@pytest.mark.parametrize(
    ("occurred_delta", "received_delta", "event_id"),
    [
        (timedelta(minutes=-1), timedelta(hours=1), "evt-z"),
        (timedelta(), timedelta(seconds=-1), "evt-z"),
        (timedelta(), timedelta(), "evt-000"),
        (timedelta(), timedelta(), "evt-001"),
    ],
)
def test_older_or_equal_event_key_is_stale_and_does_not_mutate(
    occurred_delta: timedelta,
    received_delta: timedelta,
    event_id: str,
) -> None:
    shipment = _shipment(ShipmentStatus.POSTED)
    before = replace(shipment)

    transition = shipment.apply_external_status(
        ShipmentStatus.IN_TRANSIT,
        occurred_at=NOW + occurred_delta,
        received_at=RECEIVED + received_delta,
        external_event_id=event_id,
    )

    assert transition.result is ShipmentApplicationResult.IGNORED_STALE
    assert shipment == before


def test_same_status_with_later_key_advances_ordering_without_state_change() -> None:
    shipment = _shipment(ShipmentStatus.IN_TRANSIT)
    occurred_at = NOW + timedelta(hours=1)
    received_at = occurred_at + timedelta(minutes=1)

    transition = shipment.apply_external_status(
        ShipmentStatus.IN_TRANSIT,
        occurred_at=occurred_at,
        received_at=received_at,
        external_event_id="evt-002",
    )

    assert transition.result is ShipmentApplicationResult.NO_STATE_CHANGE
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.status_occurred_at == occurred_at
    assert shipment.status_event_received_at == received_at
    assert shipment.status_external_event_id == "evt-002"
    assert shipment.updated_at == received_at


@pytest.mark.parametrize("event_id", ["", "   ", "x" * 129])
def test_external_event_id_must_be_nonempty_and_within_schema_length(
    event_id: str,
) -> None:
    shipment = _shipment()

    with pytest.raises(ValueError, match="external_event_id"):
        shipment.apply_external_status(
            ShipmentStatus.POSTED,
            occurred_at=NOW,
            received_at=RECEIVED,
            external_event_id=event_id,
        )


def test_exception_alone_does_not_set_shipped_at_but_returned_does() -> None:
    shipment = _shipment()
    shipment.apply_external_status(
        ShipmentStatus.EXCEPTION,
        occurred_at=NOW + timedelta(minutes=1),
        received_at=NOW + timedelta(minutes=2),
        external_event_id="evt-exception",
    )
    assert shipment.shipped_at is None

    returned_at = NOW + timedelta(hours=1)
    shipment.apply_external_status(
        ShipmentStatus.RETURNED,
        occurred_at=returned_at,
        received_at=returned_at + timedelta(minutes=1),
        external_event_id="evt-returned",
    )
    assert shipment.shipped_at == returned_at


def test_first_logistics_event_and_delivery_dates_are_preserved() -> None:
    shipment = _shipment()
    posted_at = NOW + timedelta(minutes=1)
    delivered_at = NOW + timedelta(hours=2)
    shipment.apply_external_status(
        ShipmentStatus.POSTED,
        occurred_at=posted_at,
        received_at=posted_at + timedelta(seconds=1),
        external_event_id="evt-posted",
    )
    shipment.apply_external_status(
        ShipmentStatus.DELIVERED,
        occurred_at=delivered_at,
        received_at=delivered_at + timedelta(seconds=1),
        external_event_id="evt-delivered",
    )

    assert shipment.shipped_at == posted_at
    assert shipment.delivered_at == delivered_at


def test_manual_cancel_is_pending_only_and_idempotent() -> None:
    shipment = _shipment()
    cancelled_at = NOW + timedelta(minutes=1)

    assert shipment.cancel(cancelled_at) is True
    assert shipment.cancel(cancelled_at + timedelta(minutes=1)) is False
    assert shipment.status is ShipmentStatus.CANCELLED
    assert shipment.status_occurred_at == cancelled_at
    assert shipment.status_event_received_at is None
    assert shipment.status_external_event_id is None
    assert shipment.updated_at == cancelled_at


@pytest.mark.parametrize(
    "status",
    [
        ShipmentStatus.POSTED,
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.EXCEPTION,
        ShipmentStatus.DELIVERED,
        ShipmentStatus.RETURNED,
    ],
)
def test_manual_cancel_rejects_every_non_pending_non_cancelled_state(
    status: ShipmentStatus,
) -> None:
    shipment = _shipment(status)

    with pytest.raises(InvalidShipmentTransitionError) as error:
        shipment.cancel(NOW + timedelta(hours=1))

    assert error.value.current is status
    assert error.value.target is ShipmentStatus.CANCELLED

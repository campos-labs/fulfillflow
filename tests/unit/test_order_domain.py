"""Complete unit coverage for the Order state machine."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fulfillflow.orders.public import InvalidOrderTransitionError, Order, OrderStatus

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _order(status: OrderStatus = OrderStatus.CREATED) -> Order:
    return Order(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        external_reference="ORDER-001",
        recipient_name="Test Recipient",
        recipient_email="recipient@example.test",
        recipient_postal_code="09700-000",
        recipient_city="Sao Bernardo do Campo",
        recipient_state="SP",
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def test_created_order_can_be_confirmed() -> None:
    order = _order()
    changed_at = NOW + timedelta(minutes=1)

    assert order.confirm(changed_at) is True
    assert order.status is OrderStatus.CONFIRMED
    assert order.updated_at == changed_at


def test_confirm_is_idempotent_without_changing_audit_time() -> None:
    order = _order(OrderStatus.CONFIRMED)

    assert order.confirm(NOW + timedelta(minutes=1)) is False
    assert order.updated_at == NOW


def test_created_order_can_be_cancelled_idempotently() -> None:
    order = _order()
    changed_at = NOW + timedelta(minutes=1)

    assert order.cancel(changed_at) is True
    assert order.cancel(changed_at + timedelta(minutes=1)) is False
    assert order.status is OrderStatus.CANCELLED
    assert order.updated_at == changed_at


def test_confirmed_order_can_be_fulfilled_idempotently() -> None:
    order = _order(OrderStatus.CONFIRMED)
    changed_at = NOW + timedelta(minutes=1)

    assert order.fulfill(changed_at) is True
    assert order.fulfill(changed_at + timedelta(minutes=1)) is False
    assert order.status is OrderStatus.FULFILLED
    assert order.updated_at == changed_at


@pytest.mark.parametrize(
    ("current", "command", "target"),
    [
        (OrderStatus.FULFILLED, "confirm", OrderStatus.CONFIRMED),
        (OrderStatus.CANCELLED, "confirm", OrderStatus.CONFIRMED),
        (OrderStatus.CONFIRMED, "cancel", OrderStatus.CANCELLED),
        (OrderStatus.FULFILLED, "cancel", OrderStatus.CANCELLED),
        (OrderStatus.CREATED, "fulfill", OrderStatus.FULFILLED),
        (OrderStatus.CANCELLED, "fulfill", OrderStatus.FULFILLED),
    ],
)
def test_other_order_transitions_are_rejected(
    current: OrderStatus,
    command: str,
    target: OrderStatus,
) -> None:
    order = _order(current)

    with pytest.raises(InvalidOrderTransitionError) as error:
        getattr(order, command)(NOW + timedelta(minutes=1))

    assert error.value.current is current
    assert error.value.target is target
    assert order.status is current
    assert order.updated_at == NOW

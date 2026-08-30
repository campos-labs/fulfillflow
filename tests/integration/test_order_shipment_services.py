"""Real PostgreSQL service, transaction and completion integration tests."""

from datetime import timedelta
from uuid import UUID

import pytest
from tests.support import ContentionProbe, FixedClock, run_with_proven_contention

from fulfillflow.db import Database
from fulfillflow.orders.public import (
    CreateOrderCommand,
    Order,
    OrderService,
    OrderStatus,
)
from fulfillflow.orders.repository import OrderRepository
from fulfillflow.shipments.public import (
    AppliedShipmentTransition,
    CreateShipmentCommand,
    ShipmentApplicationResult,
    ShipmentService,
    ShipmentStatus,
    ShipmentTrackingCodeConflictError,
)

pytestmark = pytest.mark.integration


def _order_command(reference: str) -> CreateOrderCommand:
    return CreateOrderCommand(
        external_reference=reference,
        recipient_name="Integration Recipient",
        recipient_email="integration@example.test",
        recipient_postal_code="09700-000",
        recipient_city="Sao Bernardo do Campo",
        recipient_state="SP",
    )


async def test_services_coordinate_creation_cancellation_and_database_uniqueness(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    async with postgres_database.session() as session:
        orders = OrderService(session, fixed_clock)
        order = await orders.create(_order_command("ORDER-SERVICE-001"))
        await orders.confirm(order.id)
        shipments = ShipmentService(session, fixed_clock)
        first = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", " track-001 ", None)
        )

        with pytest.raises(ShipmentTrackingCodeConflictError):
            await shipments.create(
                CreateShipmentCommand(order.id, "carrier-alpha", "TRACK-001", None)
            )

        cancelled = await shipments.cancel(first.shipment.id)
        repeated = await shipments.cancel(first.shipment.id)

    assert first.shipment.status is ShipmentStatus.PENDING
    assert first.shipment.tracking_code == "TRACK-001"
    assert cancelled.shipment.status is ShipmentStatus.CANCELLED
    assert repeated.shipment.status is ShipmentStatus.CANCELLED


async def test_last_concurrent_deliveries_complete_order_once_under_order_lock(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del carrier_id
    async with postgres_database.session() as setup_session:
        orders = OrderService(setup_session, fixed_clock)
        order = await orders.create(_order_command("ORDER-CONCURRENT-001"))
        await orders.confirm(order.id)
        shipments = ShipmentService(setup_session, fixed_clock)
        first = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", "TRACK-A", None)
        )
        second = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", "TRACK-B", None)
        )

    occurred_at = fixed_clock.current + timedelta(hours=1)
    received_at = occurred_at + timedelta(minutes=1)

    probe = ContentionProbe()
    original_get = OrderRepository.get

    async def paused_completion_lock(
        repository: OrderRepository,
        order_id: UUID,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> Order | None:
        if for_update:
            await probe.record_backend_pid(repository._session)
        order_result = await original_get(
            repository,
            order_id,
            for_update=for_update,
            for_share=for_share,
        )
        if for_update:
            await probe.hold_first_transaction()
        return order_result

    monkeypatch.setattr(OrderRepository, "get", paused_completion_lock)

    async def deliver(shipment_id: UUID, event_id: str) -> object:
        async with postgres_database.session() as session:
            return await ShipmentService(session, fixed_clock).apply_tracking_status(
                shipment_id,
                ShipmentStatus.DELIVERED,
                occurred_at=occurred_at,
                received_at=received_at,
                external_event_id=event_id,
            )

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        lambda: deliver(first.shipment.id, "evt-deliver-a"),
        lambda: deliver(second.shipment.id, "evt-deliver-b"),
    )

    async with postgres_database.session() as verify_session:
        persisted_order = await OrderService(verify_session, fixed_clock).get(order.id)
        persisted_first = await ShipmentService(verify_session, fixed_clock).get(first.shipment.id)
        persisted_second = await ShipmentService(verify_session, fixed_clock).get(
            second.shipment.id
        )

    assert all(isinstance(result, AppliedShipmentTransition) for result in results)
    transitions = [result for result in results if isinstance(result, AppliedShipmentTransition)]
    assert all(
        result.transition.result is ShipmentApplicationResult.APPLIED for result in transitions
    )
    assert sum(result.order_completed for result in transitions) == 1
    assert persisted_order.status is OrderStatus.FULFILLED
    assert persisted_first.shipment.status is ShipmentStatus.DELIVERED
    assert persisted_second.shipment.status is ShipmentStatus.DELIVERED


async def test_manual_cancellation_completes_order_when_remaining_shipment_is_delivered(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    async with postgres_database.session() as session:
        orders = OrderService(session, fixed_clock)
        order = await orders.create(_order_command("ORDER-MANUAL-CANCEL-COMPLETION"))
        await orders.confirm(order.id)
        shipments = ShipmentService(session, fixed_clock)
        delivered = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", "MANUAL-DELIVERED", None)
        )
        pending = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", "MANUAL-PENDING", None)
        )
        occurred_at = fixed_clock.current + timedelta(hours=1)
        delivery = await shipments.apply_tracking_status(
            delivered.shipment.id,
            ShipmentStatus.DELIVERED,
            occurred_at=occurred_at,
            received_at=occurred_at + timedelta(minutes=1),
            external_event_id="evt-manual-delivery",
        )

        assert delivery.order_completed is False
        cancelled = await shipments.cancel(pending.shipment.id)
        persisted_order = await orders.get(order.id)

    assert cancelled.shipment.status is ShipmentStatus.CANCELLED
    assert persisted_order.status is OrderStatus.FULFILLED


async def test_external_cancellation_completes_order_when_remaining_shipment_is_delivered(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    async with postgres_database.session() as session:
        orders = OrderService(session, fixed_clock)
        order = await orders.create(_order_command("ORDER-EXTERNAL-CANCEL-COMPLETION"))
        await orders.confirm(order.id)
        shipments = ShipmentService(session, fixed_clock)
        delivered = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", "EXTERNAL-DELIVERED", None)
        )
        pending = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", "EXTERNAL-PENDING", None)
        )
        delivered_at = fixed_clock.current + timedelta(hours=1)
        await shipments.apply_tracking_status(
            delivered.shipment.id,
            ShipmentStatus.DELIVERED,
            occurred_at=delivered_at,
            received_at=delivered_at + timedelta(minutes=1),
            external_event_id="evt-external-delivery",
        )
        cancelled_at = delivered_at + timedelta(hours=1)
        cancellation = await shipments.apply_tracking_status(
            pending.shipment.id,
            ShipmentStatus.CANCELLED,
            occurred_at=cancelled_at,
            received_at=cancelled_at + timedelta(minutes=1),
            external_event_id="evt-external-cancellation",
        )
        persisted_order = await orders.get(order.id)

    assert cancellation.transition.result is ShipmentApplicationResult.APPLIED
    assert cancellation.order_completed is True
    assert persisted_order.status is OrderStatus.FULFILLED


async def test_cancelling_every_shipment_does_not_complete_order(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
) -> None:
    del carrier_id
    async with postgres_database.session() as session:
        orders = OrderService(session, fixed_clock)
        order = await orders.create(_order_command("ORDER-ALL-CANCELLED"))
        await orders.confirm(order.id)
        shipments = ShipmentService(session, fixed_clock)
        only_shipment = await shipments.create(
            CreateShipmentCommand(order.id, "carrier-alpha", "ONLY-CANCELLED", None)
        )

        cancelled = await shipments.cancel(only_shipment.shipment.id)
        persisted_order = await orders.get(order.id)

    assert cancelled.shipment.status is ShipmentStatus.CANCELLED
    assert persisted_order.status is OrderStatus.CONFIRMED

"""Independent-session concurrency tests for Orders and Shipments."""

from datetime import timedelta
from uuid import UUID

import pytest
from tests.support import ContentionProbe, FixedClock, run_with_proven_contention

from fulfillflow.db import Database
from fulfillflow.orders.public import (
    CreateOrderCommand,
    InvalidOrderTransitionError,
    Order,
    OrderExternalReferenceConflictError,
    OrderService,
    OrderStatus,
)
from fulfillflow.orders.repository import OrderRepository
from fulfillflow.orders.schemas import OrderFilters
from fulfillflow.shipments.public import (
    AppliedShipmentTransition,
    CreateShipmentCommand,
    Shipment,
    ShipmentApplicationResult,
    ShipmentService,
    ShipmentStatus,
    ShipmentTrackingCodeConflictError,
    ShipmentView,
)
from fulfillflow.shipments.repository import ShipmentRepository
from fulfillflow.shipments.schemas import ShipmentListFilters

pytestmark = pytest.mark.integration


def _hold_first_order_state_change(
    monkeypatch: pytest.MonkeyPatch,
    probe: ContentionProbe,
) -> None:
    original_get = OrderRepository.get
    original_save = OrderRepository.save

    async def observed_get(
        repository: OrderRepository,
        order_id: UUID,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> Order | None:
        if for_update:
            await probe.record_backend_pid(repository._session)
        return await original_get(
            repository,
            order_id,
            for_update=for_update,
            for_share=for_share,
        )

    async def paused_save(repository: OrderRepository, order: Order) -> None:
        await original_save(repository, order)
        await probe.hold_first_transaction()

    monkeypatch.setattr(OrderRepository, "get", observed_get)
    monkeypatch.setattr(OrderRepository, "save", paused_save)


def _hold_first_shipment_state_change(
    monkeypatch: pytest.MonkeyPatch,
    probe: ContentionProbe,
) -> None:
    original_get = ShipmentRepository.get
    original_save = ShipmentRepository.save

    async def observed_get(
        repository: ShipmentRepository,
        shipment_id: UUID,
        *,
        for_update: bool = False,
    ) -> Shipment | None:
        if for_update:
            await probe.record_backend_pid(repository._session)
        return await original_get(repository, shipment_id, for_update=for_update)

    async def paused_save(repository: ShipmentRepository, shipment: Shipment) -> None:
        await original_save(repository, shipment)
        await probe.hold_first_transaction()

    monkeypatch.setattr(ShipmentRepository, "get", observed_get)
    monkeypatch.setattr(ShipmentRepository, "save", paused_save)


def _hold_first_order_insert(
    monkeypatch: pytest.MonkeyPatch,
    probe: ContentionProbe,
) -> None:
    original_add = OrderRepository.add

    async def paused_add(repository: OrderRepository, order: Order) -> None:
        await probe.record_backend_pid(repository._session)
        await original_add(repository, order)
        await probe.hold_first_transaction()

    monkeypatch.setattr(OrderRepository, "add", paused_add)


def _hold_first_shipment_insert(
    monkeypatch: pytest.MonkeyPatch,
    probe: ContentionProbe,
) -> None:
    original_add = ShipmentRepository.add

    async def paused_add(repository: ShipmentRepository, shipment: Shipment) -> None:
        await probe.record_backend_pid(repository._session)
        await original_add(repository, shipment)
        await probe.hold_first_transaction()

    monkeypatch.setattr(ShipmentRepository, "add", paused_add)


def _command(reference: str) -> CreateOrderCommand:
    return CreateOrderCommand(
        external_reference=reference,
        recipient_name="Concurrent Recipient",
        recipient_email="concurrent@example.test",
        recipient_postal_code="09700-000",
        recipient_city="Sao Bernardo do Campo",
        recipient_state="SP",
    )


async def _create_order(
    database: Database,
    clock: FixedClock,
    reference: str,
    *,
    confirm: bool,
) -> Order:
    async with database.session() as session:
        service = OrderService(session, clock)
        order = await service.create(_command(reference))
        return await service.confirm(order.id) if confirm else order


async def test_concurrent_confirmation_of_same_order_is_idempotent(
    postgres_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-CONFIRM",
        confirm=False,
    )

    probe = ContentionProbe()
    _hold_first_order_state_change(monkeypatch, probe)

    async def confirm() -> object:
        async with postgres_database.session() as session:
            return await OrderService(session, fixed_clock).confirm(order.id)

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        confirm,
        confirm,
    )

    assert all(isinstance(result, Order) for result in results)
    assert [result.status for result in results if isinstance(result, Order)] == [
        OrderStatus.CONFIRMED
    ] * 2
    async with postgres_database.session() as session:
        persisted = await OrderService(session, fixed_clock).get(order.id)
    assert persisted.status is OrderStatus.CONFIRMED


async def test_concurrent_cancellation_of_same_order_is_idempotent(
    postgres_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-CANCEL",
        confirm=False,
    )

    probe = ContentionProbe()
    _hold_first_order_state_change(monkeypatch, probe)

    async def cancel() -> object:
        async with postgres_database.session() as session:
            return await OrderService(session, fixed_clock).cancel(order.id)

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        cancel,
        cancel,
    )

    assert all(isinstance(result, Order) for result in results)
    assert [result.status for result in results if isinstance(result, Order)] == [
        OrderStatus.CANCELLED
    ] * 2
    async with postgres_database.session() as session:
        persisted = await OrderService(session, fixed_clock).get(order.id)
    assert persisted.status is OrderStatus.CANCELLED


async def test_concurrent_confirmation_and_cancellation_serialize_to_one_valid_winner(
    postgres_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-COMMANDS",
        confirm=False,
    )

    probe = ContentionProbe()
    _hold_first_order_state_change(monkeypatch, probe)

    async def confirm() -> object:
        async with postgres_database.session() as session:
            return await OrderService(session, fixed_clock).confirm(order.id)

    async def cancel() -> object:
        async with postgres_database.session() as session:
            return await OrderService(session, fixed_clock).cancel(order.id)

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        confirm,
        cancel,
    )
    successes = [result for result in results if isinstance(result, Order)]
    failures = [result for result in results if isinstance(result, InvalidOrderTransitionError)]

    assert len(successes) == len(failures) == 1
    assert successes[0].status is OrderStatus.CONFIRMED
    async with postgres_database.session() as session:
        persisted = await OrderService(session, fixed_clock).get(order.id)
    assert persisted.status is successes[0].status
    assert persisted.status in {OrderStatus.CONFIRMED, OrderStatus.CANCELLED}


async def test_concurrent_cancellation_of_same_shipment_is_idempotent(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del carrier_id
    order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-SHIPMENT-CANCEL",
        confirm=True,
    )
    async with postgres_database.session() as session:
        shipment = await ShipmentService(session, fixed_clock).create(
            CreateShipmentCommand(order.id, "carrier-alpha", "CONCURRENT-CANCEL", None)
        )

    probe = ContentionProbe()
    _hold_first_shipment_state_change(monkeypatch, probe)

    async def cancel() -> object:
        async with postgres_database.session() as session:
            return await ShipmentService(session, fixed_clock).cancel(shipment.shipment.id)

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        cancel,
        cancel,
    )

    assert all(isinstance(result, ShipmentView) for result in results)
    assert [result.shipment.status for result in results if isinstance(result, ShipmentView)] == [
        ShipmentStatus.CANCELLED
    ] * 2
    async with postgres_database.session() as session:
        persisted = await ShipmentService(session, fixed_clock).get(shipment.shipment.id)
        persisted_order = await OrderService(session, fixed_clock).get(order.id)
    assert persisted.shipment.status is ShipmentStatus.CANCELLED
    assert persisted_order.status is OrderStatus.CONFIRMED


async def test_concurrent_duplicate_external_reference_is_decided_by_postgresql(
    postgres_database: Database,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = ContentionProbe()
    _hold_first_order_insert(monkeypatch, probe)

    async def create() -> object:
        async with postgres_database.session() as session:
            return await OrderService(session, fixed_clock).create(
                _command("ORDER-CONCURRENT-DUPLICATE")
            )

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        create,
        create,
    )

    assert sum(isinstance(result, Order) for result in results) == 1
    assert sum(isinstance(result, OrderExternalReferenceConflictError) for result in results) == 1
    async with postgres_database.session() as session:
        page = await OrderService(session, fixed_clock).list(
            OrderFilters(external_reference="ORDER-CONCURRENT-DUPLICATE"),
            page=1,
            page_size=25,
        )
    assert page.total == len(page.items) == 1


async def test_concurrent_duplicate_carrier_tracking_code_is_decided_by_postgresql(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del carrier_id
    first_order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-TRACKING-A",
        confirm=True,
    )
    second_order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-TRACKING-B",
        confirm=True,
    )

    probe = ContentionProbe()
    _hold_first_shipment_insert(monkeypatch, probe)

    async def create(order_id: UUID) -> object:
        async with postgres_database.session() as session:
            return await ShipmentService(session, fixed_clock).create(
                CreateShipmentCommand(
                    order_id,
                    "carrier-alpha",
                    "CONCURRENT-TRACKING-DUPLICATE",
                    None,
                )
            )

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        lambda: create(first_order.id),
        lambda: create(second_order.id),
    )

    assert sum(isinstance(result, ShipmentView) for result in results) == 1
    assert sum(isinstance(result, ShipmentTrackingCodeConflictError) for result in results) == 1
    async with postgres_database.session() as session:
        page = await ShipmentService(session, fixed_clock).list(
            ShipmentListFilters(tracking_code="CONCURRENT-TRACKING-DUPLICATE"),
            page=1,
            page_size=25,
        )
    assert page.total == len(page.items) == 1


async def test_simultaneous_equal_statuses_serialize_by_total_event_key(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del carrier_id
    order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-EQUAL-STATUS",
        confirm=True,
    )
    async with postgres_database.session() as session:
        shipment = await ShipmentService(session, fixed_clock).create(
            CreateShipmentCommand(order.id, "carrier-alpha", "EQUAL-STATUS", None)
        )
    occurred_at = fixed_clock.current + timedelta(hours=1)
    received_at = occurred_at + timedelta(minutes=1)

    probe = ContentionProbe()
    _hold_first_shipment_state_change(monkeypatch, probe)

    async def apply() -> object:
        async with postgres_database.session() as session:
            return await ShipmentService(session, fixed_clock).apply_tracking_status(
                shipment.shipment.id,
                ShipmentStatus.POSTED,
                occurred_at=occurred_at,
                received_at=received_at,
                external_event_id="evt-equal",
            )

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        apply,
        apply,
    )

    assert all(isinstance(result, AppliedShipmentTransition) for result in results)
    assert {
        result.transition.result
        for result in results
        if isinstance(result, AppliedShipmentTransition)
    } == {
        ShipmentApplicationResult.APPLIED,
        ShipmentApplicationResult.IGNORED_STALE,
    }
    async with postgres_database.session() as session:
        persisted = await ShipmentService(session, fixed_clock).get(shipment.shipment.id)
    assert persisted.shipment.status is ShipmentStatus.POSTED
    assert persisted.shipment.status_external_event_id == "evt-equal"


async def test_simultaneous_different_statuses_serialize_without_lost_update(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del carrier_id
    order = await _create_order(
        postgres_database,
        fixed_clock,
        "ORDER-CONCURRENT-DIFFERENT-STATUS",
        confirm=True,
    )
    async with postgres_database.session() as session:
        shipment = await ShipmentService(session, fixed_clock).create(
            CreateShipmentCommand(order.id, "carrier-alpha", "DIFFERENT-STATUS", None)
        )
    occurred_at = fixed_clock.current + timedelta(hours=1)
    received_at = occurred_at + timedelta(minutes=1)

    probe = ContentionProbe()
    _hold_first_shipment_state_change(monkeypatch, probe)

    async def apply(
        status: ShipmentStatus,
        event_id: str,
    ) -> object:
        async with postgres_database.session() as session:
            return await ShipmentService(session, fixed_clock).apply_tracking_status(
                shipment.shipment.id,
                status,
                occurred_at=occurred_at,
                received_at=received_at,
                external_event_id=event_id,
            )

    results = await run_with_proven_contention(
        postgres_database,
        probe,
        lambda: apply(ShipmentStatus.POSTED, "evt-a"),
        lambda: apply(ShipmentStatus.DELIVERED, "evt-b"),
    )

    assert all(isinstance(result, AppliedShipmentTransition) for result in results)
    assert any(
        result.transition.result is ShipmentApplicationResult.APPLIED
        and result.shipment.status is ShipmentStatus.DELIVERED
        for result in results
        if isinstance(result, AppliedShipmentTransition)
    )
    async with postgres_database.session() as session:
        persisted = await ShipmentService(session, fixed_clock).get(shipment.shipment.id)
        persisted_order = await OrderService(session, fixed_clock).get(order.id)
    assert persisted.shipment.status is ShipmentStatus.DELIVERED
    assert persisted.shipment.status_external_event_id == "evt-b"
    assert persisted_order.status is OrderStatus.FULFILLED

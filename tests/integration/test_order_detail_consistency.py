"""Concurrent PostgreSQL proof for consistent Order detail composition."""

import asyncio
from datetime import timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from tests.support import FixedClock

from fulfillflow.api.queries import OrderDetail, OrderDetailQuery
from fulfillflow.db import Database
from fulfillflow.orders.public import (
    CreateOrderCommand,
    OrderService,
    OrderStatus,
)
from fulfillflow.shipments.public import (
    AppliedShipmentTransition,
    CreateShipmentCommand,
    ShipmentService,
    ShipmentsPublic,
    ShipmentStatus,
    ShipmentSummary,
)

pytestmark = pytest.mark.integration


async def _backend_pid(connection: AsyncConnection) -> int:
    return cast(int, await connection.scalar(text("SELECT pg_backend_pid()")))


async def _wait_until_blocked_by(
    database: Database,
    *,
    blocked_pid: int,
    blocker_pid: int,
) -> None:
    async with database.engine.connect() as monitor:
        async with asyncio.timeout(10):
            while True:
                result = await monitor.execute(
                    text("SELECT pg_blocking_pids(:blocked_pid)"),
                    {"blocked_pid": blocked_pid},
                )
                blocking_pids = cast(list[int], result.scalar_one())
                if blocker_pid in blocking_pids:
                    return


async def test_order_detail_never_combines_stale_order_with_newer_shipments(
    postgres_database: Database,
    carrier_id: UUID,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del carrier_id
    async with postgres_database.session() as setup_session:
        orders = OrderService(setup_session, fixed_clock)
        order = await orders.create(
            CreateOrderCommand(
                external_reference="ORDER-CONSISTENT-DETAIL",
                recipient_name="Consistent Detail",
                recipient_email="detail@example.test",
                recipient_postal_code="09700-000",
                recipient_city="Sao Bernardo do Campo",
                recipient_state="SP",
            )
        )
        await orders.confirm(order.id)
        shipment = await ShipmentService(setup_session, fixed_clock).create(
            CreateShipmentCommand(order.id, "carrier-alpha", "DETAIL-TRACK", None)
        )

    detail_holds_order_lock = asyncio.Event()
    release_detail_read = asyncio.Event()
    loop = asyncio.get_running_loop()
    detail_pid_ready: asyncio.Future[int] = loop.create_future()
    delivery_pid_ready: asyncio.Future[int] = loop.create_future()
    original_summaries = ShipmentsPublic.summaries_for_order

    async def gated_summaries(
        service: ShipmentsPublic,
        order_id: UUID,
    ) -> list[ShipmentSummary]:
        detail_holds_order_lock.set()
        await release_detail_read.wait()
        return await original_summaries(service, order_id)

    monkeypatch.setattr(ShipmentsPublic, "summaries_for_order", gated_summaries)

    async def read_detail() -> OrderDetail:
        async with postgres_database.engine.connect() as connection:
            detail_pid_ready.set_result(await _backend_pid(connection))
            await connection.rollback()
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                autoflush=False,
            ) as session:
                return await OrderDetailQuery(session).get(order.id)

    async def deliver() -> AppliedShipmentTransition:
        occurred_at = fixed_clock.current + timedelta(hours=1)
        async with postgres_database.engine.connect() as connection:
            delivery_pid_ready.set_result(await _backend_pid(connection))
            await connection.rollback()
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                autoflush=False,
            ) as session:
                return await ShipmentService(session, fixed_clock).apply_tracking_status(
                    shipment.shipment.id,
                    ShipmentStatus.DELIVERED,
                    occurred_at=occurred_at,
                    received_at=occurred_at + timedelta(minutes=1),
                    external_event_id="evt-consistent-detail",
                )

    detail_task = asyncio.create_task(read_detail())
    detail_pid = await asyncio.wait_for(detail_pid_ready, timeout=10)
    await asyncio.wait_for(detail_holds_order_lock.wait(), timeout=10)
    delivery_task = asyncio.create_task(deliver())
    delivery_pid = await asyncio.wait_for(delivery_pid_ready, timeout=10)

    try:
        await _wait_until_blocked_by(
            postgres_database,
            blocked_pid=delivery_pid,
            blocker_pid=detail_pid,
        )
    except BaseException:
        release_detail_read.set()
        await asyncio.gather(detail_task, delivery_task, return_exceptions=True)
        raise
    release_detail_read.set()
    detail, delivery = await asyncio.wait_for(
        asyncio.gather(detail_task, delivery_task),
        timeout=10,
    )

    assert detail.order.status is OrderStatus.CONFIRMED
    assert [summary.status for summary in detail.shipments] == [ShipmentStatus.PENDING]
    assert delivery.order_completed is True

    async with postgres_database.session() as verify_session:
        persisted_order = await OrderService(verify_session, fixed_clock).get(order.id)
        persisted_shipment = await ShipmentService(verify_session, fixed_clock).get(
            shipment.shipment.id
        )

    assert persisted_order.status is OrderStatus.FULFILLED
    assert persisted_shipment.shipment.status is ShipmentStatus.DELIVERED

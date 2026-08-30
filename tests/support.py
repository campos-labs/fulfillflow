"""Deterministic test doubles shared across test layers."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.db import Database

type ContendedOperation = Callable[[], Awaitable[object]]


class ContentionProbe:
    """Coordinate two real transactions and expose their PostgreSQL backends."""

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self.first_pid: asyncio.Future[int] = loop.create_future()
        self.second_pid: asyncio.Future[int] = loop.create_future()
        self.first_holds_transaction = asyncio.Event()
        self.release_first = asyncio.Event()

    async def record_backend_pid(self, session: AsyncSession) -> None:
        """Record the backend of the participant using the current transaction."""
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        target = self.second_pid if self.first_holds_transaction.is_set() else self.first_pid
        if not target.done():
            target.set_result(pid)

    async def hold_first_transaction(self) -> None:
        """Keep the first participant uncommitted until contention is observed."""
        if self.first_holds_transaction.is_set():
            return
        self.first_holds_transaction.set()
        await self.release_first.wait()


async def _wait_until_blocked_by(
    database: Database,
    *,
    blocked_pid: int,
    blocking_pid: int,
    blocked_task: asyncio.Task[object],
) -> None:
    async with database.engine.connect() as connection:
        while True:
            if blocked_task.done():
                result = blocked_task.result()
                raise AssertionError(
                    f"backend {blocked_pid} completed before it was blocked: {result!r}"
                )
            blockers = await connection.scalar(
                text("SELECT pg_blocking_pids(:blocked_pid)"),
                {"blocked_pid": blocked_pid},
            )
            if blockers is not None and blocking_pid in blockers:
                return


async def run_with_proven_contention(
    database: Database,
    probe: ContentionProbe,
    first: ContendedOperation,
    second: ContendedOperation,
) -> tuple[object, object]:
    """Run two participants after PostgreSQL proves that the second is blocked."""
    first_task = asyncio.create_task(first())
    second_task: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(probe.first_holds_transaction.wait(), timeout=10)
        first_pid = await asyncio.wait_for(asyncio.shield(probe.first_pid), timeout=10)
        second_task = asyncio.create_task(second())
        second_pid = await asyncio.wait_for(asyncio.shield(probe.second_pid), timeout=10)
        assert second_pid != first_pid
        await asyncio.wait_for(
            _wait_until_blocked_by(
                database,
                blocked_pid=second_pid,
                blocking_pid=first_pid,
                blocked_task=second_task,
            ),
            timeout=10,
        )
    except BaseException:
        probe.release_first.set()
        tasks = [first_task] if second_task is None else [first_task, second_task]
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    probe.release_first.set()
    assert second_task is not None
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)
    return results[0], results[1]


class FixedClock:
    """Mutable deterministic clock used by service and API tests."""

    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        """Return the test-controlled instant."""
        return self.current

"""Deterministic test doubles shared across test layers."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import NoReturn

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


async def _raise_after_concurrent_cleanup(
    tasks: tuple[asyncio.Task[object], ...],
    original_error: BaseException,
    *,
    cleanup_timeout_seconds: float = 10,
) -> NoReturn:
    """Cancel, conclusively await, and then expose the original and cleanup failures."""
    for task in tasks:
        if not task.done():
            task.cancel()

    _, pending = await asyncio.wait(tasks, timeout=cleanup_timeout_seconds)
    cleanup_error: TimeoutError | None = None
    if pending:
        pending_names = ", ".join(sorted(task.get_name() for task in pending))
        cleanup_error = TimeoutError(
            f"concurrent task cleanup exceeded {cleanup_timeout_seconds} seconds: {pending_names}"
        )
        for task in pending:
            if not task.done():
                task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    incomplete = [task.get_name() for task in tasks if not task.done()]
    if incomplete:
        incomplete_error = AssertionError(
            f"concurrent task cleanup returned with unfinished tasks: {incomplete}"
        )
        errors: list[BaseException] = [original_error]
        if cleanup_error is not None:
            errors.append(cleanup_error)
        errors.append(incomplete_error)
        raise BaseExceptionGroup("concurrent operation and cleanup failed", errors)

    if cleanup_error is not None:
        raise BaseExceptionGroup(
            "concurrent operation failed and cleanup exceeded its timeout",
            [original_error, cleanup_error],
        )
    raise original_error.with_traceback(original_error.__traceback__)


async def run_with_proven_contention(
    database: Database,
    probe: ContentionProbe,
    first: ContendedOperation,
    second: ContendedOperation,
) -> tuple[object, object]:
    """Run two participants after PostgreSQL proves that the second is blocked."""
    first_task = asyncio.create_task(first(), name="contention-first")
    second_task: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(probe.first_holds_transaction.wait(), timeout=10)
        first_pid = await asyncio.wait_for(asyncio.shield(probe.first_pid), timeout=10)
        second_task = asyncio.create_task(second(), name="contention-second")
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
        probe.release_first.set()
        results = await asyncio.wait_for(
            asyncio.gather(first_task, second_task, return_exceptions=True),
            timeout=10,
        )
        assert first_task.done()
        assert second_task.done()
        return results[0], results[1]
    except BaseException as original_error:
        probe.release_first.set()
        tasks = (first_task,) if second_task is None else (first_task, second_task)
        await _raise_after_concurrent_cleanup(tasks, original_error)


class FixedClock:
    """Mutable deterministic clock used by service and API tests."""

    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        """Return the test-controlled instant."""
        return self.current

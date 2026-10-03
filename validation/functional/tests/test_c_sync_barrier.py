"""Hook contract tests; these do not qualify a real transaction or hard-kill."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from validation.functional.c_sync_barrier import gate_after
from validation.functional.support import InstrumentationFailure


@pytest.mark.asyncio
async def test_original_failure_is_preserved_without_barrier():
    error = RuntimeError("original application failure")
    original = AsyncMock(side_effect=error)
    observe = AsyncMock()
    wrapped = gate_after(original, lambda args: True, observe)
    with pytest.raises(RuntimeError) as result:
        await wrapped(object(), "event")
    assert result.value is error
    observe.assert_not_called()


@pytest.mark.asyncio
async def test_unrelated_operation_is_not_intercepted():
    original = AsyncMock(return_value="result")
    observe = AsyncMock()
    wrapped = gate_after(original, lambda args: args == ("target",), observe)
    assert await wrapped(object(), "other") == "result"
    observe.assert_not_called()


@pytest.mark.asyncio
async def test_barrier_after_operation_and_flush_no_commit():
    order = []

    async def operation(owner, event):
        order.append("operation")
        return event

    async def flush():
        order.append("flush")

    async def observe(session):
        order.append("barrier")

    session = SimpleNamespace(
        in_transaction=Mock(return_value=True),
        flush=flush,
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    wrapped = gate_after(operation, lambda args: True, observe)
    owner = SimpleNamespace(_session=session)
    assert await wrapped(owner, "target") == "target"
    assert order == ["operation", "flush", "barrier"]
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_no_transaction_rejects_false_boundary():
    observe = AsyncMock()
    owner = SimpleNamespace(_session=SimpleNamespace(in_transaction=lambda: False))
    wrapped = gate_after(AsyncMock(), lambda args: True, observe)
    with pytest.raises(InstrumentationFailure):
        await wrapped(owner, "target")
    observe.assert_not_called()


@pytest.mark.asyncio
async def test_observer_error_cannot_become_product_success():
    owner = SimpleNamespace(
        _session=SimpleNamespace(in_transaction=lambda: True, flush=AsyncMock())
    )
    wrapped = gate_after(AsyncMock(), lambda args: True, AsyncMock(side_effect=OSError()))
    with pytest.raises(InstrumentationFailure):
        await wrapped(owner, "target")

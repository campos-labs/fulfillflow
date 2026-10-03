"""Child-local synchronous-version hooks; never edit frozen source files."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from validation.functional.support import InstrumentationFailure

Observer = Callable[[Any], Awaitable[None]]
Method = Callable[..., Awaitable[Any]]
Selector = Callable[[tuple[Any, ...]], bool]


def gate_after(original: Method, matches: Selector, observe: Observer) -> Method:
    """Run the real operation first; a failed operation never reports a reached barrier."""
    entered = False

    async def wrapped(owner: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal entered
        result = await original(owner, *args, **kwargs)
        if not matches(args):
            return result
        if entered:
            raise InstrumentationFailure("probe_second_item")
        session = owner._session
        if not session.in_transaction():
            raise InstrumentationFailure("probe_transaction_missing")
        entered = True
        try:
            # Flush remaining ORM work; no commit, rollback or substitute processing.
            await session.flush()
            await observe(session)
        except InstrumentationFailure:
            raise
        except Exception as exc:
            raise InstrumentationFailure("probe_instrumentation_failed", exc) from None
        return result

    return wrapped


@contextmanager
def installed(tag: str, event_id: str, observe: Observer) -> Iterator[None]:
    """Install only in the isolated API child before any request; restore on normal exit."""
    if tag == "v1.0.0":
        from fulfillflow.tracking.repository import TrackingRepository

        target: Any = TrackingRepository
        name = "save_inbox"

        def matches(args: tuple[Any, ...]) -> bool:
            return (
                len(args) == 1
                and str(args[0].external_event_id) == event_id
                and args[0].status.value == "PROCESSED"
            )
    elif tag == "v1.1.0-rc.1":
        from fulfillflow.shipments.receipt_repository import ShipmentReceipts

        target = ShipmentReceipts
        name = "finalize"

        def matches(args: tuple[Any, ...]) -> bool:
            return len(args) == 2 and str(args[0].external_event_id) == event_id
    else:
        raise ValueError("UNSUPPORTED_SYNC_REFERENCE")
    original = getattr(target, name)
    setattr(target, name, gate_after(original, matches, observe))
    try:
        yield
    finally:
        setattr(target, name, original)

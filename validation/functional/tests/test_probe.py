"""The observer cannot convert its own failure into a committed product BLOCKED item."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from validation.functional import probe


class Session:
    def __init__(self, transaction: bool = True, nested: bool = True) -> None:
        self.transaction = transaction
        self.nested = nested
        self.flushed = False

    def in_transaction(self) -> bool:
        return self.transaction

    def in_nested_transaction(self) -> bool:
        return self.nested

    async def flush(self) -> None:
        self.flushed = True


async def test_real_handler_result_is_observed_before_return_and_never_committed() -> None:
    session = Session()
    calls: list[str] = []
    reports: list[tuple[BaseException, str]] = []
    message = SimpleNamespace(event_id="target")

    async def handler(db: Any, item: Any, _clock: Any) -> None:
        assert db is session and item is message
        calls.append("handler")

    async def barrier(db: Any, item: Any) -> None:
        assert db is session and item is message and db.flushed
        calls.append("barrier")

    async def report(exc: BaseException, origin: str) -> None:
        reports.append((exc, origin))

    gate = probe.gated_handler(handler, barrier, "target", report)
    await gate(session, message, None)
    assert calls == ["handler", "barrier"]
    assert reports == []
    with pytest.raises(probe.InstrumentationFailure, match="probe_second_item"):
        await gate(session, message, None)
    assert calls == ["handler", "barrier"]
    assert reports[0][1] == "instrumentation"


@pytest.mark.parametrize("export_fails", [False, True])
async def test_product_exception_preserves_identity_and_skips_barrier(export_fails: bool) -> None:
    original = RuntimeError("secret product text")
    reports: list[tuple[BaseException, str]] = []
    session = Session()

    async def handler(_db: Any, _item: Any, _clock: Any) -> None:
        raise original

    async def barrier(_db: Any, _item: Any) -> None:
        pytest.fail("product failure must not reach barrier")

    async def report(exc: BaseException, origin: str) -> None:
        reports.append((exc, origin))
        if export_fails:
            raise OSError("secondary export failure")

    with pytest.raises(RuntimeError) as caught:
        await probe.gated_handler(handler, barrier, "target", report)(
            session, SimpleNamespace(event_id="target"), None
        )
    assert caught.value is original
    assert reports == [(original, "application")]
    assert not session.flushed


@pytest.mark.parametrize("export_fails", [False, True])
async def test_observer_exception_aborts_as_baseexception(export_fails: bool) -> None:
    original = PermissionError(13, "secret observer text")
    reports: list[tuple[BaseException, str]] = []

    async def handler(_db: Any, _item: Any, _clock: Any) -> None:
        return None

    async def barrier(_db: Any, _item: Any) -> None:
        raise original

    async def report(exc: BaseException, origin: str) -> None:
        reports.append((exc, origin))
        if export_fails:
            raise OSError("secondary export failure")

    with pytest.raises(probe.InstrumentationFailure) as caught:
        await probe.gated_handler(handler, barrier, "target", report)(
            Session(), SimpleNamespace(event_id="target"), None
        )
    assert not isinstance(caught.value, Exception)
    assert caught.value.code == "probe_instrumentation_failed"
    evidence = caught.value.cause_evidence
    assert evidence is not None
    frames = evidence["frames"]
    assert frames and all(frame["file"] == "validation/functional/probe.py" for frame in frames)
    assert all(isinstance(frame["line"], int) and frame["line"] > 0 for frame in frames)
    assert {key: value for key, value in evidence.items() if key != "frames"} == {
        "stage": "probe",
        "exception_type": "PermissionError",
        "errno": 13,
    }
    assert reports == [(caught.value, "instrumentation")]


async def test_explicit_instrumentation_abort_is_reported_without_replacement() -> None:
    original = probe.InstrumentationFailure("probe_barrier_not_released")
    reports: list[tuple[BaseException, str]] = []

    async def handler(_db: Any, _item: Any, _clock: Any) -> None:
        return None

    async def barrier(_db: Any, _item: Any) -> None:
        raise original

    async def report(exc: BaseException, origin: str) -> None:
        reports.append((exc, origin))

    with pytest.raises(probe.InstrumentationFailure) as caught:
        await probe.gated_handler(handler, barrier, "target", report)(
            Session(), SimpleNamespace(event_id="target"), None
        )
    assert caught.value is original
    assert reports == [(original, "instrumentation")]


@pytest.mark.parametrize(
    ("event", "transaction", "nested", "code"),
    [
        ("foreign", True, True, "probe_wrong_target"),
        ("target", False, True, "probe_transaction_missing"),
        ("target", True, False, "probe_transaction_missing"),
    ],
)
async def test_wrong_target_or_missing_transaction_does_not_call_product(
    event: str,
    transaction: bool,
    nested: bool,
    code: str,
) -> None:
    reports: list[tuple[BaseException, str]] = []

    async def handler(_db: Any, _item: Any, _clock: Any) -> None:
        pytest.fail("invalid instrumentation must not apply product work")

    async def barrier(_db: Any, _item: Any) -> None:
        pytest.fail("invalid instrumentation must not signal barrier")

    async def report(exc: BaseException, origin: str) -> None:
        reports.append((exc, origin))

    with pytest.raises(probe.InstrumentationFailure, match=code):
        await probe.gated_handler(handler, barrier, "target", report)(
            Session(transaction, nested), SimpleNamespace(event_id=event), None
        )
    assert reports[0][1] == "instrumentation"

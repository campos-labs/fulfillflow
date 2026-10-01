"""Tests of evidence publication and process supervision; no database or Docker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest
from validation.functional import support


@pytest.fixture
async def child() -> AsyncIterator[subprocess.Popen[bytes]]:
    process = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        yield process
    finally:
        await support.hard_kill(process)


def test_error_evidence_omits_secrets_and_keeps_native_codes() -> None:
    exc = PermissionError(13, "secret-password", "postgres://user:secret@host/db")
    exc.winerror = 5
    exc.__cause__ = RuntimeError("private-webhook")
    result = support.controlled_error(exc, "barrier")
    assert result == {
        "stage": "barrier",
        "exception_type": "PermissionError",
        "errno": 13,
        "winerror": 5,
    }
    assert "secret" not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_uncontrolled_labels_and_messages_are_not_exported() -> None:
    external = type("SensitiveCredentials", (RuntimeError,), {})
    result = support.controlled_error(external("private"), "token=secret")
    assert result == {"stage": "unknown", "exception_type": "OtherError"}
    assert support.controlled_error(support.ControlledFailure("marker_timeout"), "marker") == {
        "stage": "marker",
        "exception_type": "ControlledFailure",
        "code": "marker_timeout",
    }
    with pytest.raises(ValueError, match="unsupported controlled"):
        support.ControlledFailure("untrusted-secret")


def test_json_publication_is_complete_and_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "new directory" / "result.json"
    support.write_json(target, {"status": "PASS", "counter": 1})
    original = target.read_bytes()
    assert json.loads(original) == {"counter": 1, "status": "PASS"}
    assert support.sha256_file(target) == hashlib.sha256(original).hexdigest()
    with pytest.raises(FileExistsError):
        support.write_json(target, {"status": "FAIL"})
    assert target.read_bytes() == original
    assert list(target.parent.iterdir()) == [target]


def test_export_failure_keeps_os_error_and_removes_partial_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_source: Path, _destination: Path) -> None:
        raise PermissionError(13, "controlled injection")

    monkeypatch.setattr(support.os, "link", denied)
    with pytest.raises(PermissionError) as captured:
        support.write_json(tmp_path / "result.json", {"status": "PARTIAL"})
    assert captured.value.errno == 13
    assert list(tmp_path.iterdir()) == []


def test_serialization_failure_does_not_create_destination(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    with pytest.raises(ValueError):
        support.write_json(target, {"invalid": float("nan")})
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_deadline_is_monotonic_and_bounds_global_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    instant = [10.0]
    monkeypatch.setattr(support.time, "monotonic", lambda: instant[0])
    deadline = support.Deadline(5)
    assert deadline.bounded(30) == 5
    instant[0] = 14
    assert deadline.remaining() == 1
    assert deadline.bounded(0.5) == 0.5
    instant[0] = 16
    assert deadline.remaining() == 0
    with pytest.raises(support.ControlledFailure, match="deadline_expired"):
        deadline.bounded(10)


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_deadline_rejects_unbounded_or_invalid_budget(seconds: float) -> None:
    with pytest.raises(support.ControlledFailure, match="invalid_timeout"):
        support.Deadline(seconds)


async def test_wait_until_returns_observed_value() -> None:
    calls = 0

    async def observe() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "DONE"} if calls == 2 else {}

    assert await support.wait_until(observe, 1, interval=0.001) == {"status": "DONE"}
    assert calls == 2


async def test_wait_until_bounds_stalled_predicate_and_preserves_internal_timeout() -> None:
    cancelled = asyncio.Event()

    async def stalled() -> bool:
        try:
            await asyncio.Event().wait()
            return False
        finally:
            cancelled.set()

    with pytest.raises(support.ControlledFailure, match="predicate_timeout"):
        await support.wait_until(stalled, 0.02)
    assert cancelled.is_set()

    async def native_failure() -> bool:
        raise TimeoutError("original timeout")

    with pytest.raises(TimeoutError, match="original timeout"):
        await support.wait_until(native_failure, 1)


async def test_marker_loopback_accepts_one_line_without_consuming_following_line(
    child: subprocess.Popen[bytes],
) -> None:
    connections: list[asyncio.StreamWriter] = []

    async def emit(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)
        writer.write(b'{"case":"B-CORE","pid":123}\n{"next":true}\n')
        await writer.drain()

    server = await asyncio.start_server(emit, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        assert await support.read_marker(reader, child, 1) == {"case": "B-CORE", "pid": 123}
        assert await reader.readline() == b'{"next":true}\n'
    finally:
        writer.close()
        await writer.wait_closed()
        for connection in connections:
            connection.close()
            await connection.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "marker_missing"),
        (b'{"incomplete":', "marker_missing"),
        (b"not JSON\n", "marker_malformed"),
        (b'{"value":NaN}\n', "marker_malformed"),
        (b'{"value":1,"value":2}\n', "marker_malformed"),
        (b"\xff\n", "marker_malformed"),
        (b"[]\n", "marker_not_object"),
        (b"x" * support.MARKER_LIMIT, "marker_oversized"),
    ],
)
async def test_marker_invalid_input_has_controlled_diagnostic(
    child: subprocess.Popen[bytes], payload: bytes, code: str
) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    with pytest.raises(support.ControlledFailure) as captured:
        await support.read_marker(reader, child, 1)
    assert captured.value.code == code


async def test_marker_timeout_does_not_terminate_live_child(
    child: subprocess.Popen[bytes],
) -> None:
    with pytest.raises(support.ControlledFailure, match="marker_timeout"):
        await support.read_marker(asyncio.StreamReader(), child, 0.02)
    assert child.returncode is None


async def test_marker_detects_child_exit_without_waiting_for_marker(
    child: subprocess.Popen[bytes],
) -> None:
    async def stop() -> None:
        await asyncio.sleep(0.01)
        await support.hard_kill(child)

    stopping = asyncio.create_task(stop())
    with pytest.raises(support.ControlledFailure, match="child_exited"):
        await support.read_marker(asyncio.StreamReader(), child, 5)
    await stopping
    with pytest.raises(support.ControlledFailure, match="child_exited"):
        await support.read_marker(asyncio.StreamReader(), child, 5)


async def test_hard_kill_reaps_owned_child_and_does_not_kill_other_child(
    child: subprocess.Popen[bytes],
) -> None:
    other = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        result = await support.hard_kill(child)
        assert result["pid"] == child.pid
        assert result["killed"] is True
        assert result["exit_code"] == child.returncode
        assert child.returncode is not None and child.returncode != 0
        if os.name != "nt":
            assert child.returncode == -9
        assert other.returncode is None
        repeated = await support.hard_kill(child)
        assert repeated["killed"] is False
        assert repeated["exit_code"] == child.returncode
    finally:
        await support.hard_kill(other)


async def test_marker_accepts_exact_limit_and_rejects_one_byte_over(
    child: subprocess.Popen[bytes],
) -> None:
    prefix, suffix = b'{"padding":"', b'"}\n'
    payload = prefix + b"x" * (support.MARKER_LIMIT - len(prefix) - len(suffix)) + suffix
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    result = await support.read_marker(reader, child, 1)
    assert len(result["padding"]) == support.MARKER_LIMIT - len(prefix) - len(suffix)
    oversized = asyncio.StreamReader()
    oversized.feed_data(payload[:-1] + b" \n")
    with pytest.raises(support.ControlledFailure, match="marker_oversized"):
        await support.read_marker(oversized, child, 1)


def test_selector_event_loop_supports_tcp_marker_and_owned_hard_kill() -> None:
    async def scenario() -> None:
        process = await asyncio.to_thread(
            subprocess.Popen,
            [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        connection_done = asyncio.Event()

        async def emit(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                writer.write(b'{"stage":"barrier"}\n')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                connection_done.set()

        server = await asyncio.start_server(emit, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        try:
            assert await support.read_marker(reader, process, 1) == {"stage": "barrier"}
            result = await support.hard_kill(process)
            assert result["killed"] is True
            assert process.poll() is not None
            await asyncio.wait_for(connection_done.wait(), 1)
        finally:
            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()
            await support.hard_kill(process)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(scenario())


def test_instrumentation_failure_is_not_caught_as_product_item_error() -> None:
    original = PermissionError(13, "secret-password", "secret-path")
    failure = support.InstrumentationFailure("probe_instrumentation_failed", original)
    assert not isinstance(failure, Exception)
    assert support.controlled_error(failure, "probe") == {
        "stage": "probe",
        "exception_type": "InstrumentationFailure",
        "code": "probe_instrumentation_failed",
        "instrumentation_cause": {
            "stage": "probe",
            "exception_type": "PermissionError",
            "errno": 13,
        },
    }
    assert "secret" not in json.dumps(support.controlled_error(failure, "probe"))
    with pytest.raises(ValueError, match="unsupported instrumentation"):
        support.InstrumentationFailure("marker_timeout")


def test_source_identity_checks_every_imported_product_module(tmp_path: Path) -> None:
    root = tmp_path / "source with spaces"
    package = root / "src" / "fulfillflow"
    package.mkdir(parents=True)
    init = package / "__init__.py"
    init.write_text("")
    handler = package / "handler.py"
    handler.write_text("")
    base, child_module = ModuleType("fulfillflow"), ModuleType("fulfillflow.handler")
    base.__file__, child_module.__file__ = str(init), str(handler)
    selected = {"fulfillflow": base, "fulfillflow.handler": child_module}
    assert support.verify_application_source(root, selected) == {
        "fulfillflow": str(init.resolve()),
        "fulfillflow.handler": str(handler.resolve()),
    }
    external = tmp_path / "foreign.py"
    external.write_text("")
    child_module.__file__ = str(external)
    with pytest.raises(support.InstrumentationFailure, match="probe_source_mismatch"):
        support.verify_application_source(root, selected)
    child_module.__file__ = str(package / "missing.py")
    with pytest.raises(support.InstrumentationFailure, match="probe_source_mismatch"):
        support.verify_application_source(root, selected)
    del child_module.__file__
    with pytest.raises(support.InstrumentationFailure, match="probe_source_mismatch"):
        support.verify_application_source(root, selected)
    with pytest.raises(support.InstrumentationFailure, match="probe_source_missing"):
        support.verify_application_source(root, {})
    with pytest.raises(support.InstrumentationFailure, match="probe_source_missing"):
        support.verify_application_source(root / "missing", selected)


async def test_terminal_probe_error_is_preserved_after_child_exit(
    child: subprocess.Popen[bytes],
) -> None:
    await support.hard_kill(child)
    reader = asyncio.StreamReader()
    evidence = {
        "marker": "probe_error",
        "pid": child.pid,
        "token": "test-identity",
        "owner": "core",
        "event_id": "test-event",
        "origin": "instrumentation",
        "error": {"code": "probe_instrumentation_failed"},
    }
    reader.feed_data(json.dumps(evidence).encode() + b"\n")
    reader.feed_eof()
    assert await support.read_marker(reader, child, 1) == evidence


async def test_barrier_marker_never_supersedes_child_exit(
    child: subprocess.Popen[bytes],
) -> None:
    await support.hard_kill(child)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"marker":"handler_sql_before_done_and_commit"}\n')
    with pytest.raises(support.ControlledFailure, match="child_exited"):
        await support.read_marker(reader, child, 1)


async def test_terminal_diagnostic_can_arrive_one_tick_after_exit(
    child: subprocess.Popen[bytes],
) -> None:
    await support.hard_kill(child)
    reader = asyncio.StreamReader()
    asyncio.get_running_loop().call_later(
        0.01, reader.feed_data, b'{"marker":"probe_error","pid":123}\n'
    )
    assert await support.read_marker(reader, child, 1) == {
        "marker": "probe_error",
        "pid": 123,
    }


def test_database_error_keeps_category_and_sqlstate_but_no_sql_or_payload() -> None:
    from psycopg.errors import UndefinedTable
    from sqlalchemy.exc import ProgrammingError

    native = UndefinedTable("private relation and connection data")
    error = ProgrammingError(
        "SELECT private_column FROM private_table", {"secret": "value"}, native
    )
    result = support.controlled_error(error, "preparation")
    assert result == {
        "stage": "preparation",
        "exception_type": "ProgrammingError",
        "sqlstate": "42P01",
        "causes": [{"exception_type": "UndefinedTable", "sqlstate": "42P01"}],
    }
    assert "private" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    assert "SELECT" not in json.dumps(result)


@pytest.mark.parametrize("invalid", ["secret", "23505-secret", "42p01", "", None, 23505])
def test_driver_sqlstate_is_validated(invalid: object) -> None:
    from psycopg.errors import UndefinedTable

    error = UndefinedTable("do not disclose")
    error.sqlstate = invalid
    result = support.controlled_error(error, "preparation")
    assert result == {"stage": "preparation", "exception_type": "UndefinedTable"}


def test_exception_chains_are_typed_bounded_and_cycle_safe() -> None:
    from psycopg.errors import UndefinedColumn
    from sqlalchemy.exc import ProgrammingError

    nested = ProgrammingError("private sql", {}, UndefinedColumn("private column"))
    nested.__cause__ = nested
    outer = RuntimeError("private outer")
    outer.__cause__ = nested
    result = support.controlled_error(outer, "preparation")
    assert result == {
        "stage": "preparation",
        "exception_type": "RuntimeError",
        "sqlstate": "42703",
        "causes": [
            {"exception_type": "ProgrammingError"},
            {"exception_type": "UndefinedColumn", "sqlstate": "42703"},
        ],
    }
    previous: BaseException | None = None
    for _ in range(5):
        error = ProgrammingError("private sql", {}, None)
        error.__cause__ = previous
        previous = error
    assert previous is not None
    assert len(support.controlled_error(previous, "test")["causes"]) == 3


def test_unknown_driver_subclass_uses_known_base_without_exporting_class_name() -> None:
    from psycopg import ProgrammingError

    custom = type("SecretBusinessIdentifier", (ProgrammingError,), {})
    error = custom("secret contents")
    result = support.controlled_error(error, "preparation")
    assert result == {"stage": "preparation", "exception_type": "ProgrammingError"}
    assert "Secret" not in json.dumps(result)


def test_amqp_category_and_reply_code_are_controlled() -> None:
    from aiormq.exceptions import ChannelPreconditionFailed

    error = ChannelPreconditionFailed("secret-vhost", "secret-password")
    error.reply_code = 406
    assert support.controlled_error(error, "preparation") == {
        "stage": "preparation",
        "exception_type": "ChannelPreconditionFailed",
        "reply_code": 406,
    }
    error.reply_code = "secret"
    assert "reply_code" not in support.controlled_error(error, "preparation")


def test_infrastructure_error_is_filtered_without_circular_import() -> None:
    from validation.functional.environment import InfrastructureError

    error = InfrastructureError("stop_rabbit", "container_still_running", exit_code=2)
    assert support.controlled_error(error, "shutdown") == {
        "stage": "shutdown",
        "exception_type": "InfrastructureError",
        "infrastructure_stage": "stop_rabbit",
        "reason": "container_still_running",
        "exit_code": 2,
    }
    unexpected = InfrastructureError("secret-stage", "secret-reason", errno=13)
    assert support.controlled_error(unexpected, "shutdown") == {
        "stage": "shutdown",
        "exception_type": "InfrastructureError",
        "infrastructure_stage": "unknown",
        "reason": "unknown",
        "errno": 13,
    }


async def test_diagnostic_coordinates_only_include_tool_code_without_traceback_text() -> None:
    async def stalled() -> bool:
        await asyncio.Event().wait()
        return False

    with pytest.raises(support.ControlledFailure) as caught:
        await support.wait_until(stalled, 0.01)
    diagnostic = support.controlled_error(caught.value, "observation")
    assert diagnostic["frames"]
    for frame in diagnostic["frames"]:
        assert set(frame) == {"file", "line"}
        assert frame["file"] == "validation/functional/support.py"
        assert isinstance(frame["line"], int) and frame["line"] > 0
    rendered = json.dumps(diagnostic)
    assert "C:" not in rendered
    assert "test_support.py" not in rendered
    assert "stalled" not in rendered
    assert "asyncio" not in rendered

"""Bounded process supervision and controlled evidence for functional verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

MARKER_LIMIT = 8192
_ERROR_CODES = frozenset(
    {
        "deadline_expired",
        "invalid_timeout",
        "invalid_interval",
        "predicate_timeout",
        "marker_timeout",
        "marker_missing",
        "marker_malformed",
        "marker_oversized",
        "marker_not_object",
        "child_exited",
        "kill_timeout",
        "probe_source_mismatch",
        "probe_source_missing",
        "probe_instrumentation_failed",
        "probe_wrong_target",
        "probe_second_item",
        "probe_transaction_missing",
        "probe_barrier_not_released",
    }
)
_STAGES = frozenset(
    {
        "preflight",
        "identity",
        "preparation",
        "barrier",
        "marker",
        "intervention",
        "rollback",
        "recovery",
        "idempotency",
        "observation",
        "export",
        "shutdown",
        "supervision",
        "test",
        "unknown",
        "probe",
    }
)
_EXCEPTION_NAMES = frozenset(
    {
        "OSError",
        "PermissionError",
        "FileExistsError",
        "FileNotFoundError",
        "ProcessLookupError",
        "ConnectionError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
        "TimeoutError",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "RuntimeError",
        "CancelledError",
        "AssertionError",
        "ControlledFailure",
        "InstrumentationFailure",
    }
)


_LIBRARY_ERRORS = {
    "sqlalchemy": frozenset(
        {
            "SQLAlchemyError",
            "DBAPIError",
            "DatabaseError",
            "DataError",
            "IntegrityError",
            "InterfaceError",
            "InternalError",
            "OperationalError",
            "ProgrammingError",
            "NotSupportedError",
            "StatementError",
            "InvalidRequestError",
            "ArgumentError",
            "NoResultFound",
            "MultipleResultsFound",
            "ResourceClosedError",
            "PendingRollbackError",
            "NoSuchColumnError",
            "NoSuchModuleError",
            "ObjectNotExecutableError",
            "CompileError",
            "TimeoutError",
            "DisconnectionError",
            "InvalidatePoolError",
            "MissingGreenlet",
            "AwaitRequired",
            "UnboundExecutionError",
            "FlushError",
            "StaleDataError",
            "DetachedInstanceError",
        }
    ),
    "psycopg": frozenset(
        {
            "Error",
            "DatabaseError",
            "DataError",
            "IntegrityError",
            "InterfaceError",
            "InternalError",
            "OperationalError",
            "ProgrammingError",
            "NotSupportedError",
            "UniqueViolation",
            "ForeignKeyViolation",
            "NotNullViolation",
            "CheckViolation",
            "UndefinedTable",
            "UndefinedColumn",
            "DuplicateTable",
            "DuplicateDatabase",
            "InvalidCatalogName",
            "InvalidSchemaName",
            "InvalidPassword",
            "InsufficientPrivilege",
            "SerializationFailure",
            "DeadlockDetected",
            "LockNotAvailable",
            "QueryCanceled",
            "AdminShutdown",
            "TooManyConnections",
            "InFailedSqlTransaction",
        }
    ),
    "aiormq": frozenset(
        {
            "AMQPError",
            "AMQPException",
            "AMQPConnectionError",
            "ConnectionClosed",
            "ConnectionNotAllowed",
            "ConnectionNotImplemented",
            "IncompatibleProtocolError",
            "AuthenticationError",
            "ProbableAuthenticationError",
            "ChannelClosed",
            "ChannelInvalidStateError",
            "ChannelNotFoundEntity",
            "ChannelAccessRefused",
            "ChannelLockedResource",
            "ChannelPreconditionFailed",
            "DeliveryError",
            "PublishError",
            "DuplicateConsumerTag",
            "MethodNotImplemented",
        }
    ),
    "aio_pika": frozenset({"QueueEmpty", "MessageProcessError", "AMQPException"}),
    "pydantic_core": frozenset({"ValidationError"}),
}
_INFRA_REASONS = frozenset(
    {
        "docker_executable_missing",
        "startup_deadline_exceeded",
        "external_command_failed",
        "command_reap_timeout",
        "command_timeout",
        "command_os_error",
        "process_start_failed",
        "invalid_command_encoding",
        "invalid_json_response",
        "output_not_owned",
        "start_already_attempted",
        "output_already_exists",
        "invalid_container_identity",
        "image_identity_mismatch",
        "invalid_network_identity",
        "container_not_running",
        "container_unhealthy",
        "invalid_server_identity",
        "linux_engine_required",
        "volume_already_exists",
        "volume_identity_mismatch",
        "ownership_mismatch",
        "container_not_owned",
        "invalid_loopback_binding",
        "container_still_running",
    }
)
_INFRA_STAGES = frozenset(
    {
        "export",
        "prepare",
        "inventory",
        "network_create",
        "readiness",
        "docker_server",
        *(
            f"{operation}_{role}"
            for operation in (
                "image",
                "readiness",
                "volume_check",
                "volume",
                "volume_identity",
                "start",
                "inspect",
                "ports",
                "stop",
                "logs",
                "exec",
            )
            for role in ("postgres", "rabbit")
        ),
    }
)


def _library_error_name(exc: BaseException) -> str | None:
    # Do not export arbitrary subclass names. Known bases still identify an
    # unlisted driver subclass, while SQLSTATE preserves its concrete category.
    for cls in type(exc).__mro__:
        family = cls.__module__.split(".", 1)[0]
        if cls.__name__ in _LIBRARY_ERRORS.get(family, frozenset()):
            return cls.__name__
    return None


def _error_fields(exc: BaseException) -> dict[str, object]:
    name = type(exc).__name__
    known = _library_error_name(exc)
    fields: dict[str, object] = {
        "exception_type": known or (name if name in _EXCEPTION_NAMES else "OtherError")
    }
    if known:
        state = getattr(exc, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
            fields["sqlstate"] = state
        reply = getattr(exc, "reply_code", None)
        if type(reply) is int and 0 <= reply <= 999:
            fields["reply_code"] = reply
    if isinstance(exc, OSError):
        for key in ("errno", "winerror"):
            value = getattr(exc, key, None)
            if type(value) is int:
                fields[key] = value
    return fields


def _local_frames(exc: BaseException) -> list[dict[str, object]]:
    """Coordinates only: never source text, locals, function names or external paths."""
    tool = Path(__file__).resolve().parent
    roots = [(tool, "validation/functional"), (tool.parents[1] / "src", "src")]
    package = sys.modules.get("fulfillflow")
    imported = getattr(package, "__file__", None)
    if isinstance(imported, str):
        location = Path(imported).resolve()
        if location.parent.name == "fulfillflow" and location.parent.parent.name == "src":
            roots.append((location.parent.parent, "src"))
    frames: list[dict[str, object]] = []
    current = exc.__traceback__
    while current is not None:
        location = Path(current.tb_frame.f_code.co_filename).resolve()
        for root, label in roots:
            if location.is_relative_to(root):
                relative = location.relative_to(root)
                allowed = (
                    relative.parent == Path(".")
                    if label == "validation/functional"
                    else relative.parts[0] == "fulfillflow"
                )
                if allowed and relative.suffix == ".py" and location.is_file():
                    frames.append(
                        {"file": f"{label}/{relative.as_posix()}", "line": current.tb_lineno}
                    )
                break
        current = current.tb_next
    return frames[-6:]


class ControlledFailure(RuntimeError):
    """Failure whose code can be exported without including external content."""

    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("unsupported controlled failure code")
        self.code = code
        super().__init__(code)


class InstrumentationFailure(BaseException):
    """Abort the transaction without entering the product's item-error handler.

    The frozen store catches Exception and persists BLOCKED for unexpected item
    errors. A failed observer must instead roll back and remain a tooling failure.
    """

    def __init__(self, code: str, cause: BaseException | None = None) -> None:
        if code not in _ERROR_CODES or not code.startswith("probe_"):
            raise ValueError("unsupported instrumentation failure code")
        self.code = code
        self.cause_evidence = controlled_error(cause, "probe") if cause is not None else None
        super().__init__(code)


def verify_application_source(
    source: Path, modules: dict[str, ModuleType] | None = None
) -> dict[str, str]:
    """Check the actual imported package tree before opening product dependencies."""
    expected = (source.resolve() / "src" / "fulfillflow").resolve()
    selected = sys.modules if modules is None else modules
    if "fulfillflow" not in selected or not expected.is_dir():
        raise InstrumentationFailure("probe_source_missing")
    observed = {}
    for name, module in selected.items():
        if name != "fulfillflow" and not name.startswith("fulfillflow."):
            continue
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            raise InstrumentationFailure("probe_source_mismatch")
        path = Path(filename).resolve()
        if not path.is_relative_to(expected) or not path.is_file():
            raise InstrumentationFailure("probe_source_mismatch")
        observed[name] = str(path)
    return observed


def controlled_error(exc: BaseException, stage: str) -> dict[str, object]:
    """Finite typed diagnostics; never exception messages, SQL, args or external paths."""
    result: dict[str, object] = {
        "stage": stage if stage in _STAGES else "unknown",
        **_error_fields(exc),
    }
    if isinstance(exc, (ControlledFailure, InstrumentationFailure)):
        result["code"] = exc.code
    if isinstance(exc, InstrumentationFailure) and exc.cause_evidence is not None:
        result["instrumentation_cause"] = exc.cause_evidence
    if type(exc).__name__ == "InfrastructureError" and type(exc).__module__ in (
        "environment",
        "validation.functional.environment",
    ):
        result["exception_type"] = "InfrastructureError"
        reason, infrastructure_stage = getattr(exc, "reason", None), getattr(exc, "stage", None)
        result["reason"] = (
            reason if isinstance(reason, str) and reason in _INFRA_REASONS else "unknown"
        )
        result["infrastructure_stage"] = (
            infrastructure_stage
            if isinstance(infrastructure_stage, str) and infrastructure_stage in _INFRA_STAGES
            else "unknown"
        )
        for key in ("exit_code", "errno", "winerror"):
            value = getattr(exc, key, None)
            if type(value) is int:
                result[key] = value
    causes: list[dict[str, object]] = []
    visited = {id(exc)}
    pending = [getattr(exc, "orig", None), exc.__cause__]
    while pending and len(causes) < 3:
        cause = pending.pop(0)
        if not isinstance(cause, BaseException) or id(cause) in visited:
            continue
        visited.add(id(cause))
        if _library_error_name(cause) is not None:
            fields = _error_fields(cause)
            causes.append(fields)
            if "sqlstate" in fields and "sqlstate" not in result:
                result["sqlstate"] = fields["sqlstate"]
            pending.extend([getattr(cause, "orig", None), cause.__cause__])
    if causes:
        result["causes"] = causes
    frames = _local_frames(exc)
    if frames:
        result["frames"] = frames
    return result


def write_json(path: Path, value: object) -> None:
    """Publish a complete JSON file atomically and refuse an existing destination.

    A same-directory hard link provides atomic create-if-absent on NTFS/POSIX.
    Filesystems without hard-link support fail explicitly rather than overwriting.
    """
    encoded = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".functional-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve the primary export failure; never replace its cause.
        raise
    else:
        temporary.unlink()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _positive_seconds(seconds: float) -> None:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ControlledFailure("invalid_timeout")


class Deadline:
    """Monotonic deadline whose remaining budget cannot become negative."""

    def __init__(self, seconds: float) -> None:
        _positive_seconds(seconds)
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def bounded(self, seconds: float) -> float:
        _positive_seconds(seconds)
        remaining = self.remaining()
        if remaining <= 0:
            raise ControlledFailure("deadline_expired")
        return min(seconds, remaining)


async def wait_until[T](
    predicate: Callable[[], Awaitable[T]], seconds: float, interval: float = 0.1
) -> T:
    """Return the first truthy observation, bounding even a stalled predicate."""
    _positive_seconds(seconds)
    if not math.isfinite(interval) or interval <= 0:
        raise ControlledFailure("invalid_interval")
    guard = asyncio.timeout(seconds)
    try:
        async with guard:
            while True:
                result = await predicate()
                if result:
                    return result
                await asyncio.sleep(interval)
    except TimeoutError:
        if guard.expired():
            raise ControlledFailure("predicate_timeout") from None
        raise


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


async def _read_json_line(reader: asyncio.StreamReader) -> dict[str, Any]:
    data = bytearray()
    while len(data) < MARKER_LIMIT:
        chunk = await reader.read(1)
        if not chunk:
            raise ControlledFailure("marker_missing")
        data.extend(chunk)
        if chunk == b"\n":
            break
    else:
        raise ControlledFailure("marker_oversized")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ControlledFailure("marker_malformed") from None
    if not isinstance(value, dict):
        raise ControlledFailure("marker_not_object")
    return value


async def _wait_for_exit(process: subprocess.Popen[bytes]) -> int:
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            return exit_code
        await asyncio.sleep(0.1)


async def read_marker(
    reader: asyncio.StreamReader, process: subprocess.Popen[bytes], seconds: float
) -> dict[str, Any]:
    """Read a bounded marker while preserving a terminal probe diagnostic.

    The caller validates token/PID/event/owner even for probe_error messages. A
    barrier marker never supersedes process exit. After exit, allow at most one
    supervision interval (and never beyond the given deadline) for buffered TCP
    diagnostics; startup errors sent only to stderr remain the caller's evidence.
    """
    deadline = Deadline(seconds)
    marker_task = asyncio.create_task(_read_json_line(reader))
    exit_task = asyncio.create_task(_wait_for_exit(process))
    try:
        done, _pending = await asyncio.wait(
            (marker_task, exit_task),
            timeout=deadline.remaining(),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if exit_task in done or process.poll() is not None:
            try:
                # Reading may complete just after the OS reports process exit.
                if not marker_task.done() and deadline.remaining() > 0:
                    await asyncio.wait_for(
                        asyncio.shield(marker_task), min(0.1, deadline.remaining())
                    )
                if marker_task.done() and not marker_task.cancelled():
                    marker = marker_task.result()
                    if marker.get("marker") == "probe_error":
                        return marker
            except (ControlledFailure, OSError, TimeoutError):
                pass  # Keep process exit primary when no valid diagnostic arrived.
            raise ControlledFailure("child_exited")
        if marker_task in done:
            return marker_task.result()
        raise ControlledFailure("marker_timeout")
    finally:
        marker_task.cancel()
        exit_task.cancel()
        await asyncio.gather(marker_task, exit_task, return_exceptions=True)


async def hard_kill(process: subprocess.Popen[bytes], seconds: float = 15) -> dict[str, object]:
    """Force-terminate and reap only the subprocess object provided by its owner."""
    _positive_seconds(seconds)
    killed = False
    if process.poll() is None:
        try:
            process.kill()
            killed = True
        except ProcessLookupError:
            pass  # It can exit between the returncode check and the OS call.
    try:
        exit_code = await asyncio.to_thread(process.wait, timeout=seconds)
    except subprocess.TimeoutExpired:
        raise ControlledFailure("kill_timeout") from None
    return {"pid": process.pid, "exit_code": exit_code, "killed": killed}

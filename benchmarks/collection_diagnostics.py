"""Bounded, allowlisted diagnostics for resource collection failures only."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIX = "RESOURCE_FAILURE_JSON="
STAGES = frozenset(
    {
        "output",
        "endpoint",
        "connections",
        "snapshot",
        "delta",
        "write",
        "transport",
        "normalize",
        "arguments",
    }
)


def counters(value: object) -> dict[str, int | str]:
    """Never retain raw Docker responses or arbitrary exception values."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, int | str] = {}
    for key in ("cpu", "system", "cpus", "memory", "limit"):
        item = value.get(key)
        if type(item) is int:
            result[key] = item
    stamp = value.get("read")
    if isinstance(stamp, str) and len(stamp) <= 64:
        try:
            if datetime.fromisoformat(stamp).utcoffset() is not None:
                result["read"] = stamp
        except ValueError:
            pass
    return result


def failure(error: BaseException, stage: str, started: float) -> dict[str, Any]:
    """Keep exception classes and process status, never exception text or argv."""
    chain: list[dict[str, Any]] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        name = type(current).__name__
        item: dict[str, Any] = {
            "type": name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception",
        }
        if isinstance(current, OSError):
            for key in ("errno", "winerror"):
                code = getattr(current, key, None)
                if type(code) is int:
                    item[key] = code
        if isinstance(current, subprocess.CalledProcessError):
            item["returncode"] = current.returncode
            stderr = current.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, str):
                for line in stderr.splitlines():
                    if line.startswith(PREFIX):
                        try:
                            helper = json.loads(line[len(PREFIX) :])
                            if isinstance(helper, dict) and helper.get("stage") in STAGES:
                                item["helper_stage"] = helper["stage"]
                                errors = helper.get("errors", [])
                                if (
                                    isinstance(errors, list)
                                    and errors
                                    and isinstance(errors[0], dict)
                                ):
                                    kind = errors[0].get("type", "")
                                    if isinstance(kind, str) and re.fullmatch(
                                        r"[A-Za-z_][A-Za-z0-9_]{0,79}", kind
                                    ):
                                        item["helper_exception_type"] = kind
                                projected = []
                                if isinstance(errors, list):
                                    for entry in errors[:8]:
                                        if not isinstance(entry, dict):
                                            continue
                                        kind = entry.get("type")
                                        if not isinstance(kind, str) or not re.fullmatch(
                                            r"[A-Za-z_][A-Za-z0-9_]{0,79}", kind
                                        ):
                                            continue
                                        safe = {"type": kind}
                                        for key in ("errno", "winerror", "returncode"):
                                            if type(entry.get(key)) is int:
                                                safe[key] = entry[key]
                                        projected.append(safe)
                                if projected:
                                    item["helper_errors"] = projected
                        except (ValueError, TypeError):
                            pass
        if isinstance(current, subprocess.TimeoutExpired):
            item["timed_out"] = True
        chain.append(item)
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return {
        "schema_version": 1,
        "stage": stage if stage in STAGES else "snapshot",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "duration_seconds": max(0.0, time.monotonic() - started),
        "message": "mandatory resource collection failed",
        "errors": chain,
    }


def write_failure(path: Path, report: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")

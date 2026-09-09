"""Sanitized operational diagnostics, retained before owned infrastructure is removed."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from benchmarks.collectors import EnvironmentMismatchError


def sanitize(value: str) -> str:
    for key, secret in os.environ.items():
        if secret and re.search(r"password|secret|token|signature|database_url", key, re.I):
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "[redacted URL]", value, flags=re.I)
    lines = []
    for line in value.splitlines():
        if re.search(r"raw_body|parsed_payload|authorization|signature|webhook.body", line, re.I):
            lines.append("[redacted sensitive line]")
        else:
            line = re.sub(r"(?i)(password|secret|token)\s*[=:]\s*\S+", r"\1=[redacted]", line)
            lines.append(line)
    return "\n".join(lines)[-12000:]


def error_report(error: BaseException) -> dict[str, object]:
    chain = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        item: dict[str, object] = {"type": type(current).__name__}
        if isinstance(current, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
            # Exception.__str__ includes the complete argv, which may carry credentials.
            item["message"] = "external command failed or exceeded its deadline"
            item["returncode"] = getattr(current, "returncode", None)
            stderr = current.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            item["stderr"] = sanitize(stderr)
        else:
            item["message"] = sanitize(str(current))
        if isinstance(current, EnvironmentMismatchError):
            item["checks"] = json.loads(sanitize(json.dumps(current.report)))
        chain.append(item)
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return {"errors": chain}


def write_report(path: Path, report: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def diagnostics(compose: list[str], destination: Path, *, include_runtime: bool = False) -> None:
    from benchmarks.collectors import run_capture

    destination.mkdir(parents=True, exist_ok=False)
    failed = False
    for name, argv in (
        ("containers", [*compose, "ps", "--all", "--format", "json"]),
        ("logs", [*compose, "logs", "--no-color", "--tail", "200"]),
    ):
        try:
            output = run_capture(argv, 30)
            (destination / f"{name}.txt").write_text(
                sanitize(output.stdout + output.stderr), encoding="utf-8"
            )
        except Exception as exc:
            write_report(destination / f"{name}-error.json", error_report(exc))
            failed = True
    if failed:
        raise RuntimeError("diagnostic export incomplete; preserve resources for manual review")
    if include_runtime:
        identifier = run_capture([*compose, "ps", "-q", "loadgen"], 30).stdout.strip()
        if identifier:
            exists = run_capture(
                [
                    "docker",
                    "exec",
                    identifier,
                    "python",
                    "-c",
                    "from pathlib import Path; print(Path('/tmp/fulfillflow-benchmark').is_dir())",
                ],
                30,
            ).stdout.strip()
            if exists == "True":
                # Only runner manifests, synthetic dataset and phase evidence live here.
                # Failed phases may not have reached the runner's normal export path.
                run_capture(
                    [
                        "docker",
                        "cp",
                        f"{identifier}:/tmp/fulfillflow-benchmark/.",
                        str(destination / "loadgen-runtime"),
                    ],
                    30,
                )

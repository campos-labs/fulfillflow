"""Dependency readiness is distinct from a cheap periodic interpreter check."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest


def _service(name: str) -> str:
    compose = Path("compose.benchmark.yaml").read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", compose, re.M | re.S)
    assert match is not None
    return match[1]


def _startup_command() -> list[str]:
    match = re.search(r"^    command: (\[.*\])$", _service("loadgen-init"), re.M)
    assert match is not None
    return cast(list[str], json.loads(match[1]))


def test_loadgen_cannot_start_until_same_image_dependency_check_succeeds() -> None:
    startup, loadgen = _service("loadgen-init"), _service("loadgen")
    assert "<<: *loadgen-image" in startup and "<<: *loadgen-image" in loadgen
    assert 'profiles: ["campaign"]' in startup and 'profiles: ["campaign"]' in loadgen
    assert 'entrypoint: ["python"]' in startup
    assert _startup_command() == ["-c", "import locust"]
    assert "healthcheck:\n      disable: true" in startup
    assert 'restart: "no"' in startup
    assert "loadgen-init:\n        condition: service_completed_successfully" in loadgen
    assert "app:\n        condition: service_healthy" in loadgen
    assert 'test: ["CMD", "python", "-c", "pass"]' in loadgen
    for parameter in ("BENCH_LOADGEN_CPUS", "BENCH_LOADGEN_MEMORY"):
        assert parameter in startup and parameter in loadgen
    for timing in ("interval: 5s", "timeout: 5s", "retries: 12", "start_period: 5s"):
        assert timing in loadgen


@pytest.mark.parametrize("dependencies_available", [True, False])
def test_real_startup_import_exit_controls_readiness(dependencies_available: bool) -> None:
    # -S excludes site packages without changing the installed environment.
    flags = [] if dependencies_available else ["-I", "-S"]
    result = subprocess.run(
        [sys.executable, *flags, *_startup_command()],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert (result.returncode == 0) is dependencies_available
    if not dependencies_available:
        assert b"ModuleNotFoundError" in result.stderr


def test_periodic_check_does_not_replace_initial_dependency_validation() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", "pass"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0

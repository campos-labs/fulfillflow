from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks.collectors import ExternalCommandError
from benchmarks.prepare_database import (
    PreparationError,
    _seed_exact_database,
    cleanup_database,
    prepare_database,
)
from benchmarks.run_campaign import _validate_prepare_command
from benchmarks.seed_loader import SeedSafetyError

MANIFEST = Path("benchmarks/fixtures/smoke-campaign.json")
PROJECT = "fulfillflow-benchmark"
DATABASE = "fulfillflow_benchmark"


@pytest.fixture(autouse=True)
def benchmark_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "BENCH_DATABASE_URL",
        "postgresql+psycopg://benchmark-user:synthetic-value@db/fulfillflow_benchmark",
    )
    monkeypatch.setenv("BENCH_POSTGRES_DB", DATABASE)
    monkeypatch.setenv("BENCH_POSTGRES_USER", "benchmark-user")
    monkeypatch.setenv("BENCH_POSTGRES_PASSWORD", "synthetic-value")


def completed(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


def test_preparation_uses_scoped_argv_and_verifies_after_atomic_seed() -> None:
    commands: list[list[str]] = []
    verified: list[str] = []

    def runner(command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return completed(command)

    prepare_database(
        MANIFEST,
        project_name=PROJECT,
        confirmed_project_name=PROJECT,
        database_name=DATABASE,
        confirmed_database_name=DATABASE,
        timeout_seconds=85.0,
        cleanup_timeout_seconds=20.0,
        command_runner=runner,
        verifier=lambda bundle, _root, _runner, _timeout: verified.append(
            bundle.manifest.environment.compose_project
        ),
    )

    assert verified == [PROJECT]
    assert len(commands) == 4
    assert all(isinstance(command, list) for command in commands)
    assert all(command[:3] == ["docker", "compose", "--project-name"] for command in commands)
    assert all(command[3] == PROJECT for command in commands)
    assert "config" in commands[0]
    assert commands[1][-3:] == ["down", "--volumes", "--remove-orphans"]
    assert commands[1][commands[1].index("--profile") + 1] == "campaign"
    assert "up" in commands[2]
    assert "--no-build" in commands[2]
    assert "--wait" in commands[2]
    assert "run" in commands[3]
    assert "--rm" in commands[3]
    assert "--no-deps" in commands[3]
    assert "--internal-seed" in commands[3]
    assert not any("postgresql+psycopg" in item for command in commands for item in command)


def test_runner_accepts_the_documented_preparation_argv() -> None:
    _validate_prepare_command(
        [
            r".venv\Scripts\python.exe",
            "-B",
            "-m",
            "benchmarks.prepare_database",
            "--manifest",
            "benchmarks/campaigns/campaign.json",
            "--project-name",
            PROJECT,
            "--confirm-project-name",
            PROJECT,
            "--database-name",
            DATABASE,
            "--confirm-database-name",
            DATABASE,
            "--timeout-seconds",
            "85",
            "--cleanup-timeout-seconds",
            "20",
        ]
    )


def test_every_preparation_begins_by_removing_the_same_project_volume() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return completed(command)

    for _ in range(2):
        prepare_database(
            MANIFEST,
            project_name=PROJECT,
            confirmed_project_name=PROJECT,
            database_name=DATABASE,
            confirmed_database_name=DATABASE,
            timeout_seconds=85.0,
            cleanup_timeout_seconds=20.0,
            command_runner=runner,
            verifier=lambda *_args: None,
        )

    down_commands = [command for command in commands if "down" in command]
    assert len(down_commands) == 2
    assert all("--volumes" in command for command in down_commands)
    assert all("campaign" in command for command in down_commands)


@pytest.mark.parametrize(
    ("project", "confirmation"),
    [
        ("fulfillflow", "fulfillflow"),
        ("fulfillflow-demo", "fulfillflow-demo"),
        (PROJECT, "different-project"),
        ("fulfillflow-task08-prepare-e2e-short", "fulfillflow-task08-prepare-e2e-short"),
    ],
)
def test_unsafe_or_ambiguous_project_is_refused_before_subprocess(
    project: str,
    confirmation: str,
) -> None:
    calls: list[list[str]] = []

    with pytest.raises(PreparationError, match="project"):
        prepare_database(
            MANIFEST,
            project_name=project,
            confirmed_project_name=confirmation,
            database_name=DATABASE,
            confirmed_database_name=DATABASE,
            timeout_seconds=85.0,
            cleanup_timeout_seconds=20.0,
            command_runner=lambda command, _timeout: calls.append(command),  # type: ignore[arg-type,return-value]
            verifier=lambda *_args: None,
        )

    assert calls == []


def test_external_database_target_is_refused_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "BENCH_DATABASE_URL",
        "postgresql+psycopg://benchmark-user:synthetic-value@external/fulfillflow_benchmark",
    )
    calls: list[list[str]] = []

    with pytest.raises(PreparationError, match="Compose-local"):
        prepare_database(
            MANIFEST,
            project_name=PROJECT,
            confirmed_project_name=PROJECT,
            database_name=DATABASE,
            confirmed_database_name=DATABASE,
            timeout_seconds=85.0,
            cleanup_timeout_seconds=20.0,
            command_runner=lambda command, _timeout: calls.append(command),  # type: ignore[arg-type,return-value]
            verifier=lambda *_args: None,
        )

    assert calls == []


def test_partial_failure_is_sanitized_and_cleans_the_isolated_project() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "up" in command:
            raise ExternalCommandError("synthetic-sensitive-value")
        return completed(command)

    with pytest.raises(PreparationError) as captured:
        prepare_database(
            MANIFEST,
            project_name=PROJECT,
            confirmed_project_name=PROJECT,
            database_name=DATABASE,
            confirmed_database_name=DATABASE,
            timeout_seconds=85.0,
            cleanup_timeout_seconds=20.0,
            command_runner=runner,
            verifier=lambda *_args: None,
        )

    assert "synthetic-sensitive-value" not in str(captured.value)
    assert sum("down" in command for command in commands) == 2
    assert commands[-1][-3:] == ["down", "--volumes", "--remove-orphans"]


def test_cleanup_requires_literal_safe_project_and_removes_only_its_resources() -> None:
    commands: list[list[str]] = []

    cleanup_database(
        project_name="fulfillflow-task08-prepare-e2e-1234abcd",
        confirmed_project_name="fulfillflow-task08-prepare-e2e-1234abcd",
        timeout_seconds=20.0,
        command_runner=lambda command, _timeout: commands.append(command) or completed(command),
    )

    assert len(commands) == 1
    assert commands[0][3] == "fulfillflow-task08-prepare-e2e-1234abcd"
    assert commands[0][-3:] == ["down", "--volumes", "--remove-orphans"]
    assert commands[0][commands[0].index("--profile") + 1] == "campaign"


def test_internal_seed_requires_a_fresh_database_and_authenticates_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "5" * 64
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://benchmark-user:synthetic-value@db/fulfillflow_benchmark",
    )
    monkeypatch.setattr(
        "benchmarks.prepare_database.verify_benchmark_artifacts", lambda _path: digest
    )

    async def loaded(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(inserted=True, logical_hash=digest)

    monkeypatch.setattr("benchmarks.prepare_database.load_dataset", loaded)

    assert _seed_exact_database(DATABASE) == digest


def test_internal_seed_failure_does_not_render_sensitive_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://benchmark-user:synthetic-sensitive@db/fulfillflow_benchmark",
    )
    monkeypatch.setattr(
        "benchmarks.prepare_database.verify_benchmark_artifacts", lambda _path: "5" * 64
    )

    async def refused(*_args: object, **_kwargs: object) -> object:
        raise SeedSafetyError("synthetic-sensitive")

    monkeypatch.setattr("benchmarks.prepare_database.load_dataset", refused)

    with pytest.raises(PreparationError) as captured:
        _seed_exact_database(DATABASE)

    assert "synthetic-sensitive" not in str(captured.value)

"""Restore one isolated benchmark Compose project to the frozen database state."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from benchmarks.campaign import CampaignBundle, load_campaign
from benchmarks.collectors import (
    CommandRunner,
    DatabaseProbe,
    DockerProbe,
    run_capture,
)
from benchmarks.dataset import verify_benchmark_artifacts
from benchmarks.seed_loader import load_dataset

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_FILE = REPOSITORY_ROOT / "compose.benchmark.yaml"
_OFFICIAL_PROJECT = "fulfillflow-benchmark"
_E2E_PROJECT = re.compile(r"^fulfillflow-task08-prepare-e2e-[a-z0-9]{8,32}$")
_DATABASE_NAME = "fulfillflow_benchmark"


class PreparationError(RuntimeError):
    """Sanitized preparation failure that never renders command output or credentials."""


Verifier = Callable[[CampaignBundle, Path, CommandRunner, float], None]


def prepare_database(
    manifest_path: Path,
    *,
    project_name: str,
    confirmed_project_name: str,
    database_name: str,
    confirmed_database_name: str,
    timeout_seconds: float,
    cleanup_timeout_seconds: float,
    command_runner: CommandRunner = run_capture,
    verifier: Verifier | None = None,
) -> None:
    """Replace the project volume, seed it, and verify the complete initial state."""
    try:
        manifest_path = manifest_path.resolve()
        _validate_manifest_path(manifest_path)
        bundle = load_campaign(manifest_path)
        _validate_target(
            bundle,
            project_name=project_name,
            confirmed_project_name=confirmed_project_name,
            database_name=database_name,
            confirmed_database_name=confirmed_database_name,
        )
    except PreparationError:
        raise
    except Exception:
        raise PreparationError("benchmark preparation contract was refused") from None
    runtime_bundle = _bundle_for_project(bundle, project_name)
    compose = _compose_prefix(project_name)
    try:
        command_runner([*compose, "config", "--quiet"], timeout_seconds)
    except Exception:
        raise PreparationError("benchmark Compose configuration was refused") from None

    cleanup = [
        *compose,
        "--profile",
        "campaign",
        "down",
        "--volumes",
        "--remove-orphans",
    ]
    try:
        command_runner(cleanup, cleanup_timeout_seconds)
        command_runner(
            [
                *compose,
                "--profile",
                "campaign",
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                str(max(1, int(timeout_seconds))),
                "--no-build",
            ],
            timeout_seconds,
        )
        command_runner(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "--volume",
                f"{REPOSITORY_ROOT}:/workspace:ro",
                "--workdir",
                "/workspace",
                "app",
                "python",
                "-B",
                "-m",
                "benchmarks.prepare_database",
                "--internal-seed",
                "--confirm-database-name",
                database_name,
            ],
            timeout_seconds,
        )
        (verifier or _verify_prepared)(
            runtime_bundle,
            REPOSITORY_ROOT,
            command_runner,
            timeout_seconds,
        )
    except Exception:
        try:
            command_runner(cleanup, cleanup_timeout_seconds)
        except Exception:
            raise PreparationError(
                "benchmark preparation failed and isolated cleanup could not be confirmed"
            ) from None
        raise PreparationError(
            "benchmark preparation failed safely; isolated cleanup completed"
        ) from None


def cleanup_database(
    *,
    project_name: str,
    confirmed_project_name: str,
    timeout_seconds: float,
    command_runner: CommandRunner = run_capture,
) -> None:
    """Remove only one explicitly confirmed benchmark project and its volume."""
    _validate_project_confirmation(project_name, confirmed_project_name)
    try:
        command_runner(
            [
                *_compose_prefix(project_name),
                "--profile",
                "campaign",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            timeout_seconds,
        )
    except Exception:
        raise PreparationError("isolated benchmark cleanup could not be confirmed") from None


def _verify_prepared(
    bundle: CampaignBundle,
    repository_root: Path,
    command_runner: CommandRunner,
    timeout_seconds: float,
) -> None:
    from benchmarks.run_campaign import _verify_initial_state

    observed = DockerProbe(
        bundle,
        repository_root,
        command_runner=command_runner,
    ).observe()
    database = DatabaseProbe(
        observed.container_ids["postgres"],
        observed.postgres_user,
        observed.postgres_database,
        command_runner=command_runner,
        timeout_seconds=timeout_seconds,
    )
    _verify_initial_state(bundle, database)


def _seed_exact_database(confirmed_database_name: str) -> str:
    try:
        app_env = os.environ.get("APP_ENV")
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise PreparationError("benchmark database environment is incomplete")
        _validate_database_url(
            database_url,
            confirmed_database_name,
            expected_user=None,
            expected_password=None,
        )
        digest = verify_benchmark_artifacts(REPOSITORY_ROOT / "benchmarks" / "datasets")
        result = asyncio.run(
            load_dataset(
                "benchmark",
                database_url=database_url,
                app_env=app_env or "",
                confirmed_database_name=confirmed_database_name,
            )
        )
        if not result.inserted or result.logical_hash != digest:
            raise PreparationError("fresh benchmark database was not seeded exactly once")
    except Exception:
        raise PreparationError("atomic benchmark seed was refused") from None
    return digest


def _validate_manifest_path(path: Path) -> None:
    if not path.is_file() or not path.is_relative_to(REPOSITORY_ROOT):
        raise PreparationError("manifest must be an existing file inside the repository")


def _validate_target(
    bundle: CampaignBundle,
    *,
    project_name: str,
    confirmed_project_name: str,
    database_name: str,
    confirmed_database_name: str,
) -> None:
    if (
        bundle.manifest.environment.compose_project != _OFFICIAL_PROJECT
        or bundle.manifest.environment.compose_file != _COMPOSE_FILE.name
    ):
        raise PreparationError("manifest does not select the benchmark Compose contract")
    _validate_project_confirmation(project_name, confirmed_project_name)
    if database_name != _DATABASE_NAME or confirmed_database_name != database_name:
        raise PreparationError("benchmark database target was not confirmed literally")
    database_url = os.environ.get("BENCH_DATABASE_URL")
    expected_database = os.environ.get("BENCH_POSTGRES_DB")
    expected_user = os.environ.get("BENCH_POSTGRES_USER")
    expected_password = os.environ.get("BENCH_POSTGRES_PASSWORD")
    if (
        not database_url
        or expected_database != database_name
        or not expected_user
        or not expected_password
    ):
        raise PreparationError("benchmark database environment is incomplete or divergent")
    _validate_database_url(
        database_url,
        database_name,
        expected_user=expected_user,
        expected_password=expected_password,
    )


def _validate_project_confirmation(project_name: str, confirmed_project_name: str) -> None:
    allowed = project_name == _OFFICIAL_PROJECT or _E2E_PROJECT.fullmatch(project_name) is not None
    if not allowed or confirmed_project_name != project_name:
        raise PreparationError("benchmark Compose project was not confirmed literally")


def _validate_database_url(
    value: str,
    database_name: str,
    *,
    expected_user: str | None,
    expected_password: str | None,
) -> None:
    try:
        url = make_url(value)
    except ArgumentError:
        raise PreparationError("benchmark database environment is invalid") from None
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "db"
        or url.port not in (None, 5432)
        or url.database != database_name
        or (expected_user is not None and url.username != expected_user)
        or (expected_password is not None and url.password != expected_password)
    ):
        raise PreparationError("benchmark database environment is not Compose-local")


def _bundle_for_project(bundle: CampaignBundle, project_name: str) -> CampaignBundle:
    environment = bundle.manifest.environment.model_copy(update={"compose_project": project_name})
    manifest = bundle.manifest.model_copy(update={"environment": environment})
    return replace(bundle, manifest=manifest)


def _compose_prefix(project_name: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(_COMPOSE_FILE),
    ]


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive finite number") from exc
    if not 0 < parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--project-name")
    parser.add_argument("--confirm-project-name")
    parser.add_argument("--database-name")
    parser.add_argument("--confirm-database-name")
    parser.add_argument("--timeout-seconds", type=_positive_seconds, default=85.0)
    parser.add_argument("--cleanup-timeout-seconds", type=_positive_seconds, default=20.0)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--internal-seed", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    """Run the fail-closed preparation or its isolated cleanup operation."""
    parser = _parser()
    args = parser.parse_args()
    if args.internal_seed:
        if not args.confirm_database_name:
            parser.error("--confirm-database-name is required for the internal seed")
        try:
            digest = _seed_exact_database(args.confirm_database_name)
        except PreparationError as exc:
            print(f"benchmark preparation refused: {exc}", file=sys.stderr)
            return 2
        print(f"benchmark dataset installed atomically; logical_sha256={digest}")
        return 0
    if not args.project_name or not args.confirm_project_name:
        parser.error("project name and its literal confirmation are required")
    try:
        if args.cleanup:
            cleanup_database(
                project_name=args.project_name,
                confirmed_project_name=args.confirm_project_name,
                timeout_seconds=args.cleanup_timeout_seconds,
            )
        else:
            if not args.manifest or not args.database_name or not args.confirm_database_name:
                parser.error("manifest, database name and its literal confirmation are required")
            prepare_database(
                args.manifest,
                project_name=args.project_name,
                confirmed_project_name=args.confirm_project_name,
                database_name=args.database_name,
                confirmed_database_name=args.confirm_database_name,
                timeout_seconds=args.timeout_seconds,
                cleanup_timeout_seconds=args.cleanup_timeout_seconds,
            )
    except PreparationError as exc:
        print(f"benchmark preparation refused: {exc}", file=sys.stderr)
        return 2
    action = "cleanup complete" if args.cleanup else "database prepared and verified"
    print(f"benchmark {action}; project={args.project_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

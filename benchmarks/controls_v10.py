"""Prepare exactly two non-official v1.0 mixed/4 controls in an isolated checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from benchmarks.operational_errors import error_report, sanitize, write_report

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks" / "results"
V10_REVISION = "ae15e0a2da465f4aec3d9c699655441ad1947265"
PROJECT = "fulfillflow-benchmark"
DATABASE = "fulfillflow_benchmark"
WINDOWS_BUILD = "26200.9445"
WORKTREE_NAME = "v10-controls-win9445-source"
ATTEMPT_NAMES = (
    "v10-control-mixed-4-win9445-attempt-01",
    "v10-control-mixed-4-win9445-attempt-02",
)


class ControlError(RuntimeError):
    """Fail closed without rendering command arguments or unfiltered process output."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_direct_result_child(path: Path, expected_name: str) -> bool:
    return path.name == expected_name and path.parent == RESULTS


def _validate_locations(checkout: Path, attempts: Sequence[Path]) -> None:
    if not _is_direct_result_child(checkout, WORKTREE_NAME):
        raise ControlError("isolated checkout must use the reviewed result destination")
    if len(attempts) != len(ATTEMPT_NAMES) or any(
        not _is_direct_result_child(path, name)
        for path, name in zip(attempts, ATTEMPT_NAMES, strict=True)
    ):
        raise ControlError("control destinations differ from the reviewed two-attempt plan")
    if len({checkout, *attempts}) != len(attempts) + 1:
        raise ControlError("checkout and attempt destinations must be distinct")
    if any(path.exists() for path in (checkout, *attempts)):
        raise ControlError("all destinations must be new; no automatic retry or overwrite")


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout: float | None = 30,
    evidence: Path | None = None,
    required: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(environment) if environment is not None else None,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if evidence is not None:
            write_report(evidence.with_suffix(".error.json"), error_report(exc))
        raise ControlError("required external command did not complete") from None
    if evidence is not None:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(sanitize(completed.stdout + completed.stderr), encoding="utf-8")
    if required and completed.returncode != 0:
        raise ControlError("required external command failed")
    return completed


def _git(root: Path, *args: str) -> str:
    return _run(["git", *args], cwd=root).stdout.strip()


def _source_environment(source: Path, baseline_environment: Mapping[str, str]) -> dict[str, str]:
    environment = dict(baseline_environment)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(source), str(source / "src"))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    return environment


def _source_python(source: Path) -> Path:
    executable = source / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        raise ControlError("isolated v1.0 virtual environment was not created")
    return executable


def _run_source_python(
    source: Path,
    environment: Mapping[str, str],
    code: str,
    *arguments: str,
    timeout: float | None = 30,
    evidence: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [str(_source_python(source)), "-X", "utf8", "-B", "-c", code, *arguments],
        cwd=source,
        environment=_source_environment(source, environment),
        timeout=timeout,
        evidence=evidence,
    )


def _baseline_document(source: Path) -> dict[str, Any]:
    path = source / "benchmarks" / "campaigns" / "v1-baseline-mixed.json"
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError("frozen v1.0 mixed manifest is unreadable") from exc
    if not isinstance(decoded, dict):
        raise ControlError("frozen v1.0 mixed manifest is not an object")
    document = cast(dict[str, Any], decoded)
    if (
        document.get("release") != "v1.0.0"
        or document.get("git_sha") != V10_REVISION
        or document.get("profile") != "mixed"
        or document.get("official") is not True
    ):
        raise ControlError("frozen v1.0 mixed manifest differs from the published contract")
    return document


def candidate_document(baseline: Mapping[str, Any], attempt_number: int) -> dict[str, Any]:
    """Derive the authorized control by changing only selection, status, host build and path."""
    if attempt_number not in (1, 2):
        raise ValueError("only the two approved control identifiers are valid")
    decoded = json.loads(json.dumps(baseline))
    if not isinstance(decoded, dict):  # Defensive: JSON object input must remain an object.
        raise ValueError("baseline manifest is not an object")
    document = cast(dict[str, Any], decoded)
    loads = document["loads"]
    if not isinstance(loads, list) or len(loads) < 1 or loads[0].get("users") != 4:
        raise ValueError("published mixed manifest does not contain the frozen four-user load")
    document["name"] = ATTEMPT_NAMES[attempt_number - 1]
    document["official"] = False
    document["repetitions"] = 1
    document["loads"] = [loads[0]]
    document["host"]["identity"]["os_build"] = WINDOWS_BUILD
    # Candidate manifests live below benchmarks/results in the isolated v1.0 checkout.
    document["cohorts"]["dataset_manifest"] = "../../datasets/benchmark-v1.0.json"
    return document


def _verify_candidate(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], number: int
) -> None:
    expected = candidate_document(baseline, number)
    if dict(candidate) != expected:
        raise ControlError("candidate changes workload or another frozen v1.0 parameter")


def _compose(source: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--file",
        str(source / "compose.benchmark.yaml"),
    ]


def _assert_project_absent(source: Path, environment: Mapping[str, str]) -> None:
    for arguments in (
        ["docker", "ps", "--all", "--quiet"],
        ["docker", "volume", "ls", "--quiet"],
        ["docker", "network", "ls", "--quiet"],
    ):
        completed = _run(
            [*arguments, "--filter", f"label=com.docker.compose.project={PROJECT}"],
            cwd=source,
            environment=environment,
        )
        if completed.stdout.strip():
            raise ControlError(
                "the isolated v1.0 Compose project already exists; preserve it for review"
            )


def _benchmark_environment(document: Mapping[str, Any]) -> dict[str, str]:
    pool = document["pool"]
    resources = document["resources"]
    environment = os.environ.copy()
    environment.update(
        {
            "BENCH_POSTGRES_DB": DATABASE,
            "BENCH_POSTGRES_USER": "fulfillflow",
            "BENCH_POSTGRES_PASSWORD": "v10-control-synthetic-database-2026",
            "BENCH_DATABASE_URL": "postgresql+psycopg://fulfillflow:"
            "v10-control-synthetic-database-2026@db:5432/fulfillflow_benchmark",
            "SESSION_SECRET": "v10-control-synthetic-session-secret-2026",
            "CARRIER_ALPHA_WEBHOOK_SECRET": "v10-control-synthetic-alpha-secret-2026",
            "CARRIER_BETA_WEBHOOK_SECRET": "v10-control-synthetic-beta-secret-2026",
            "BENCH_DB_POOL_SIZE": str(pool["size"]),
            "BENCH_DB_MAX_OVERFLOW": str(pool["max_overflow"]),
            "BENCH_DB_POOL_TIMEOUT_SECONDS": str(pool["timeout_seconds"]),
            "BENCH_DB_STATEMENT_TIMEOUT_MS": str(pool["statement_timeout_ms"]),
            "BENCH_LOG_LEVEL": "WARNING",
            "BENCH_LOG_FORMAT": "json",
            "BENCH_OTEL_ENABLED": "false",
            "BENCH_OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "BENCH_OTEL_TRACES_SAMPLER_ARG": "0.0",
            "BENCH_OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "BENCH_APP_PORT": "18007",
        }
    )
    for role in ("app", "postgres", "loadgen"):
        resource = resources[role]
        environment[f"BENCH_{role.upper()}_CPUS"] = str(resource["cpus"])
        environment[f"BENCH_{role.upper()}_MEMORY"] = str(resource["memory"])
    return environment


def _image_preflight(
    source: Path, document: Mapping[str, Any], environment: Mapping[str, str]
) -> dict[str, object]:
    references = {
        "app": "fulfillflow:benchmark-local",
        "loadgen": "fulfillflow-loadgen:benchmark-local",
        "postgres": f"postgres:18-trixie@{document['images']['postgres']}",
    }
    observed: dict[str, object] = {}
    for role, reference in references.items():
        completed = _run(
            ["docker", "image", "inspect", reference], cwd=source, environment=environment
        )
        try:
            inspection = json.loads(completed.stdout)[0]
            identities = {
                inspection["Id"],
                *(value.rsplit("@", 1)[-1] for value in inspection.get("RepoDigests", [])),
            }
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ControlError("frozen image identity could not be inspected") from exc
        if document["images"][role] not in identities:
            raise ControlError("an original v1.0 image tag no longer identifies the frozen image")
        observed[role] = {"reference": reference, "identity": inspection["Id"]}
    return observed


def _verify_source(source: Path, *, allow_candidates: bool = False) -> None:
    if source.resolve() == ROOT.resolve():
        raise ControlError("the active v1.1 checkout cannot be used for v1.0 controls")
    if _git(source, "rev-parse", "--show-toplevel") != str(source.resolve()):
        raise ControlError("control source is not an independent checkout")
    if _git(source, "rev-parse", "HEAD") != V10_REVISION:
        raise ControlError("control source is not the frozen v1.0 revision")
    changes = _git(source, "status", "--porcelain", "--untracked-files=all").splitlines()
    candidate_directory = "benchmarks/results/v10-controls-win9445-candidates"
    individual_candidates = {f"?? {candidate_directory}/{name}.json" for name in ATTEMPT_NAMES}
    allowed_changes = (individual_candidates, {f"?? {candidate_directory}/"})
    if (not allow_candidates and changes) or (
        allow_candidates and set(changes) not in allowed_changes
    ):
        raise ControlError("control source changed outside its generated control candidates")


def _materialize_checkout(checkout: Path, launcher: Mapping[str, Any]) -> Path:
    _run(["git", "worktree", "add", "--detach", str(checkout), V10_REVISION], cwd=ROOT, timeout=60)
    try:
        uv = shutil.which("uv")
        if uv is None:
            raise ControlError("uv is unavailable for the isolated frozen environment")
        evidence = checkout / "benchmarks" / "results" / "v10-controls-win9445-bootstrap.txt"
        _run([uv, "sync", "--frozen", "--all-groups"], cwd=checkout, timeout=180, evidence=evidence)
        _verify_source(checkout)
        write_report(
            checkout / "benchmarks" / "results" / "v10-controls-win9445-launcher.json",
            {"revision": V10_REVISION, "launcher": launcher, "uv": str(Path(uv).resolve())},
        )
        return checkout
    except BaseException as exc:
        write_report(
            checkout / "benchmarks" / "results" / "v10-controls-win9445-bootstrap-error.json",
            error_report(exc),
        )
        raise


def _write_candidates(source: Path, attempts: Sequence[Path]) -> list[Path]:
    baseline = _baseline_document(source)
    candidates = source / "benchmarks" / "results" / "v10-controls-win9445-candidates"
    candidates.mkdir(parents=True, exist_ok=False)
    result = []
    for number, attempt in enumerate(attempts, 1):
        document = candidate_document(baseline, number)
        candidate = candidates / f"{attempt.name}.json"
        write_report(candidate, document)
        _verify_candidate(baseline, json.loads(candidate.read_text(encoding="utf-8")), number)
        result.append(candidate)
    return result


def _source_host_preflight(
    source: Path, candidate: Path, environment: Mapping[str, str], evidence: Path
) -> dict[str, object]:
    completed = _run_source_python(
        source,
        environment,
        """
import json, sys
from pathlib import Path
from benchmarks.campaign import load_campaign
from benchmarks.host_probe import HostProbe
manifest = load_campaign(Path(sys.argv[1])).manifest
probe = HostProbe(manifest.host, official=False, timeout_seconds=manifest.timeouts.command_seconds)
print(json.dumps({'identity': probe.identity(), 'conditions': probe.dynamic({})}))
""",
        str(candidate),
        evidence=evidence,
    )
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ControlError("frozen HostProbe returned an invalid report") from exc
    if not isinstance(decoded, dict):
        raise ControlError("frozen HostProbe returned an invalid report")
    return cast(dict[str, object], decoded)


def _capture_diagnostics(source: Path, environment: Mapping[str, str], destination: Path) -> None:
    destination.mkdir(exist_ok=False)
    failed = False
    for name, arguments in (
        ("containers.txt", ["ps", "--all", "--format", "json"]),
        ("logs.txt", ["logs", "--no-color", "--tail", "200"]),
    ):
        try:
            _run(
                [*_compose(source), *arguments],
                cwd=source,
                environment=environment,
                evidence=destination / name,
            )
        except ControlError as exc:
            write_report(destination / f"{name}.error.json", error_report(exc))
            failed = True
    if failed:
        raise ControlError("diagnostic export is incomplete; infrastructure was preserved")


def _prepare(source: Path, candidate: Path, attempt: Path, environment: Mapping[str, str]) -> int:
    """Use the frozen migration/seed/verifier path, retaining evidence before any cleanup."""
    evidence = attempt / "preparation"
    evidence.mkdir(exist_ok=False)
    try:
        candidate_directory = source / "benchmarks" / "results" / "v10-controls-win9445-candidates"
        if (
            candidate.parent != candidate_directory
            or candidate.stem not in ATTEMPT_NAMES
            or attempt.name != candidate.stem
            or attempt.parent != RESULTS
        ):
            raise ControlError("preparation target differs from the reviewed control destinations")
        _verify_source(source, allow_candidates=True)
        _verify_candidate(
            _baseline_document(source),
            json.loads(candidate.read_text(encoding="utf-8")),
            ATTEMPT_NAMES.index(candidate.stem) + 1,
        )
        _assert_project_absent(source, environment)
        _run(
            [*_compose(source), "config", "--quiet"],
            cwd=source,
            environment=environment,
            evidence=evidence / "config.txt",
        )
        _run(
            [
                *_compose(source),
                "--profile",
                "campaign",
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "85",
                "--no-build",
                "--pull",
                "never",
            ],
            cwd=source,
            environment=environment,
            timeout=85,
            evidence=evidence / "up.txt",
        )
        _run(
            [
                *_compose(source),
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "--volume",
                f"{source}:/workspace:ro",
                "--workdir",
                "/workspace",
                "app",
                "python",
                "-B",
                "-m",
                "benchmarks.prepare_database",
                "--internal-seed",
                "--confirm-database-name",
                DATABASE,
            ],
            cwd=source,
            environment=environment,
            timeout=85,
            evidence=evidence / "seed.txt",
        )
        _run_source_python(
            source,
            environment,
            """
import sys
from pathlib import Path
from benchmarks.campaign import load_campaign
from benchmarks.collectors import run_capture
from benchmarks.prepare_database import _verify_prepared
_verify_prepared(load_campaign(Path(sys.argv[1])), Path.cwd(), run_capture, 30)
""",
            str(candidate),
            timeout=30,
            evidence=evidence / "verify.txt",
        )
        write_report(evidence / "ready.json", {"verified": True})
        return 0
    except BaseException as exc:
        write_report(evidence / "error.json", error_report(exc))
        try:
            _capture_diagnostics(source, environment, evidence / "diagnostics-before-cleanup")
        except BaseException as diagnostic_error:
            write_report(evidence / "diagnostics-error.json", error_report(diagnostic_error))
        # The runner reports the non-zero exit and stops. It never deletes failed resources.
        return 2


def _prepare_process(source: Path, candidate: Path, attempt: Path) -> list[str]:
    return [
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        "-X",
        "utf8",
        "-B",
        str(ROOT / "scripts" / "prepare_v10_control.py"),
        "--prepare",
        "--source",
        str(source),
        "--candidate",
        str(candidate),
        "--attempt",
        str(attempt),
    ]


def _write_checksums(destination: Path) -> None:
    paths = sorted(
        path
        for path in destination.rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    )
    lines = [f"{_sha256(path)}  {path.relative_to(destination).as_posix()}" for path in paths]
    (destination / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_attempt(source: Path, candidate: Path, attempt: Path, launcher: Mapping[str, Any]) -> int:
    document = json.loads(candidate.read_text(encoding="utf-8"))
    environment = _benchmark_environment(document)
    attempt.mkdir(parents=False, exist_ok=False)
    result: dict[str, object] = {
        "complete": False,
        "stage": "preflight",
        "candidate_sha256": _sha256(candidate),
    }
    code = 2
    entered = False
    try:
        _verify_source(source, allow_candidates=True)
        _verify_candidate(
            _baseline_document(source), document, ATTEMPT_NAMES.index(attempt.name) + 1
        )
        _assert_project_absent(source, environment)
        result["images"] = _image_preflight(source, document, environment)
        result["host"] = _source_host_preflight(
            source, candidate, environment, attempt / "host-preflight.txt"
        )
        result["launcher"] = dict(launcher)
        write_report(attempt / "candidate.json", document)
        prepare = _prepare_process(source, candidate, attempt)
        write_report(attempt / "preparation-argv.json", prepare)
        result["stage"] = "runner"
        entered = True
        completed = _run(
            [
                str(_source_python(source)),
                "-X",
                "utf8",
                "-B",
                "-m",
                "benchmarks.run_campaign",
                "--manifest",
                str(candidate),
                "--execute",
                "--confirm-campaign",
                document["name"],
                "--base-url",
                "http://app:8000",
                "--results-directory",
                str(attempt / "run"),
                "--prepare-command-json",
                json.dumps(prepare),
            ],
            cwd=source,
            environment=_source_environment(source, environment),
            timeout=None,
            evidence=attempt / "runner.txt",
            required=False,
        )
        code = completed.returncode
        if code != 0:
            raise ControlError("frozen runner rejected or interrupted the control")
        result["complete"] = True
    except KeyboardInterrupt:
        code = 130
        result.update(error_report(KeyboardInterrupt()))
    except BaseException as exc:
        if code == 0:
            code = 2
        result.update(error_report(exc))
    finally:
        result["exit_code"] = code
        write_report(attempt / "result.json", result)
        if entered:
            try:
                _capture_diagnostics(source, environment, attempt / "diagnostics")
                if code == 0:
                    _run(
                        [
                            *_compose(source),
                            "--profile",
                            "campaign",
                            "down",
                            "--volumes",
                            "--remove-orphans",
                        ],
                        cwd=source,
                        environment=environment,
                        timeout=30,
                        evidence=attempt / "cleanup.txt",
                    )
                else:
                    result["resources_preserved_for_review"] = True
            except BaseException as exc:
                result["diagnostic_or_cleanup_error"] = error_report(exc)
                code = 2 if code == 0 else code
                result["complete"] = False
                result["resources_preserved_for_review"] = True
        result["exit_code"] = code
        write_report(attempt / "result.json", result)
        _write_checksums(attempt)
    return code


def _plan(
    checkout: Path, attempts: Sequence[Path], launcher: Mapping[str, Any]
) -> dict[str, object]:
    _validate_locations(checkout, attempts)
    if _git(ROOT, "cat-file", "-e", f"{V10_REVISION}^{{commit}}") != "":
        raise ControlError("frozen v1.0 commit cannot be resolved")
    return {
        "revision": V10_REVISION,
        "checkout": str(checkout),
        "attempts": [str(path) for path in attempts],
        "profile": "mixed",
        "users": 4,
        "quota": 430,
        "repetitions": 1,
        "host_build": WINDOWS_BUILD,
        "launcher": dict(launcher),
        "load_executed": False,
    }


def execute(
    checkout: Path, attempts: Sequence[Path], launcher: Mapping[str, Any], *, plan_only: bool
) -> int:
    plan = _plan(checkout, attempts, launcher)
    if plan_only:
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    source = _materialize_checkout(checkout, launcher)
    candidates = _write_candidates(source, attempts)
    for candidate, attempt in zip(candidates, attempts, strict=True):
        code = _run_attempt(source, candidate, attempt, launcher)
        if code != 0:
            return code
    return 0


def _options_from_stdin() -> tuple[Path, tuple[Path, Path], dict[str, Any], bool]:
    try:
        options = json.load(sys.stdin)
        checkout = Path(options["checkout"]).resolve()
        attempts = tuple(Path(value).resolve() for value in options["attempts"])
        launcher = options["launcher"]
        plan_only = options["plan_only"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ControlError("control launcher options are invalid") from exc
    if (
        set(options) != {"checkout", "attempts", "launcher", "plan_only"}
        or len(attempts) != 2
        or not isinstance(launcher, dict)
        or type(plan_only) is not bool
    ):
        raise ControlError("control launcher options are invalid")
    return checkout, (attempts[0], attempts[1]), launcher, plan_only


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--attempt", type=Path)
    args = parser.parse_args()
    if args.prepare:
        if not args.source or not args.candidate or not args.attempt:
            parser.error("--prepare requires --source, --candidate and --attempt")
        return _prepare(
            args.source.resolve(), args.candidate.resolve(), args.attempt.resolve(), os.environ
        )
    try:
        checkout, attempts, launcher, plan_only = _options_from_stdin()
        return execute(checkout, attempts, launcher, plan_only=plan_only)
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:
        print(json.dumps(error_report(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

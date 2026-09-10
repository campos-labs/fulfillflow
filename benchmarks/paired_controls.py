"""Prepare or manually execute the approved frozen mixed/4 A1 B1 B2 A2 block."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from benchmarks.controls_v10 import (
    ControlError,
    _baseline_document,
    _benchmark_environment,
    _git,
    _run,
    _run_runner,
    _sha256,
    _source_environment,
    _source_python,
    _write_checksums,
)
from benchmarks.operational_errors import error_report, write_report

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/results"
PACKAGE = RESULTS / "paired-win9445-review-01"
JOURNAL = RESULTS / "paired-win9445-execution-01"
Version = Literal["v10", "v11"]
REVISIONS = {
    "v10": "ae15e0a2da465f4aec3d9c699655441ad1947265",
    "v11": "948cefdf881b10af2c073be5536411daefb3faf2",
}
PROJECTS = {"v10": "fulfillflow-benchmark", "v11": "fulfillflow-ii-paired-win9445"}
PILOT = RESULTS / "v11-pilot-win9445-attempt-01/pilot-manifest.json"
PILOT_SHA256 = "29f98f223dd20b6a2a2cff696c8dc4fd181c9942f725ae2bcd4de7616fec95e6"
AUDIT = RESULTS / "v11-pilot-win9445-review/loadgen-compatibility.json"
AUDIT_SHA256 = "d48890fcca373367248bc292aec52c3c6243858ffdd18ca833e67eb678d0b22e"


@dataclass(frozen=True)
class Step:
    number: int
    block: int
    version: Version
    label: str

    @property
    def name(self) -> str:
        return f"paired-mixed-4-win9445-{self.number:02d}-{self.label}-{self.version}"

    @property
    def source(self) -> Path:
        return RESULTS / f"paired-win9445-{self.version}-source-01"

    @property
    def candidate(self) -> Path:
        return PACKAGE / "candidates" / f"{self.name}.json"

    @property
    def attempt(self) -> Path:
        return RESULTS / self.name


STEPS = (
    Step(1, 1, "v10", "a1"),
    Step(2, 1, "v11", "b1"),
    Step(3, 2, "v11", "b2"),
    Step(4, 2, "v10", "a2"),
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ControlError("expected a JSON object")
    return value


def original_documents() -> dict[str, dict[str, Any]]:
    if _sha256(PILOT) != PILOT_SHA256 or _sha256(AUDIT) != AUDIT_SHA256:
        raise ControlError("original pilot manifest or loadgen audit changed")
    pilot = read_json(PILOT)
    if pilot["git_sha"] != REVISIONS["v11"] or pilot["official"]:
        raise ControlError("pilot identity differs from the approved frozen source")
    return {"v10": _baseline_document(ROOT), "v11": pilot}


def candidate_document(step: Step, originals: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(json.dumps(originals[step.version])))
    document.update(name=step.name, official=False, repetitions=1)
    document["loads"] = [load for load in document["loads"] if load["users"] == 4]
    if len(document["loads"]) != 1 or document["warmup_quota_per_shipment"] != 430:
        raise ControlError("frozen mixed/4 quota or load selection differs")
    document["host"]["identity"]["os_build"] = "26200.9445"
    dataset = step.source / "benchmarks/datasets/benchmark-v1.0.json"
    document["cohorts"]["dataset_manifest"] = os.path.relpath(
        dataset, step.candidate.parent
    ).replace("\\", "/")
    if step.version == "v11":
        document["environment"]["compose_project"] = PROJECTS["v11"]
    return document


def verify_source(step: Step) -> None:
    source = step.source.resolve()
    if (
        source == ROOT.resolve()
        or Path(_git(source, "rev-parse", "--show-toplevel")).resolve() != source
    ):
        raise ControlError("source must be an independent checkout")
    if _git(source, "rev-parse", "HEAD") != REVISIONS[step.version]:
        raise ControlError("source revision differs from the measured version")
    if _git(source, "branch", "--show-current"):
        raise ControlError("measured source must remain detached")
    if _git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ControlError("measured source contains changes")


def sync_source(step: Step, *, check: bool = False) -> None:
    executable = shutil.which("uv")
    if executable is None:
        raise ControlError("uv executable unavailable")
    environment = _source_environment(step.source, os.environ)
    environment["UV_PROJECT_ENVIRONMENT"] = str(step.source / ".venv")
    argv = [
        executable,
        "sync",
        "--frozen",
        "--all-groups",
        "--no-python-downloads",
        "--cache-dir",
        str(ROOT / ".uv-cache"),
        "--python",
        sys.executable,
    ]
    if check:
        argv += ["--offline", "--check"]
    _run(
        argv,
        cwd=step.source,
        environment=environment,
        timeout=180,
        evidence=None if check else PACKAGE / f"{step.version}-sync.txt",
    )


def environment_for(step: Step, document: Mapping[str, Any]) -> dict[str, str]:
    if step.version == "v10":
        environment = _benchmark_environment(document)
    else:
        environment = dict(os.environ)
        for line in (step.source / ".env.benchmark-v11.example").read_text().splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                environment[key] = value
        for role in ("core", "tracking", "loadgen"):
            environment[f"BENCH_{role.upper()}_IMAGE"] = document["images"][role]
    return _source_environment(step.source, environment)


def compose(step: Step) -> list[str]:
    filename = "compose.benchmark.yaml" if step.version == "v10" else "compose.benchmark-v11.yaml"
    return [
        "docker",
        "compose",
        "--project-name",
        PROJECTS[step.version],
        "--file",
        str(step.source / filename),
    ]


def assert_projects_absent() -> None:
    for project in PROJECTS.values():
        for argv in (
            ["docker", "ps", "--all", "--quiet"],
            ["docker", "volume", "ls", "--quiet"],
            ["docker", "network", "ls", "--quiet"],
        ):
            if _run(
                [*argv, "--filter", f"label=com.docker.compose.project={project}"], cwd=ROOT
            ).stdout.strip():
                raise ControlError(f"project {project} exists; preserve it for review")


def image_preflight(step: Step, document: Mapping[str, Any]) -> dict[str, Any]:
    report = {}
    for role, identifier in document["images"].items():
        reference = identifier
        if step.version == "v10" and role in {"app", "loadgen"}:
            reference = {
                "app": "fulfillflow:benchmark-local",
                "loadgen": "fulfillflow-loadgen:benchmark-local",
            }[role]
        info = json.loads(_run(["docker", "image", "inspect", reference], cwd=ROOT).stdout)[0]
        identities = {
            info["Id"],
            *(item.rsplit("@", 1)[-1] for item in info.get("RepoDigests", [])),
        }
        if identifier not in identities:
            raise ControlError(f"frozen {role} image unavailable or changed")
        if (
            role in {"core", "tracking"}
            and (info["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            != REVISIONS[step.version]
        ):
            raise ControlError(f"{role} image label differs from measured commit")
        report[role] = {"reference": reference, "identity": info["Id"]}
    return report


def source_python(
    step: Step, code: str, *args: str, evidence: Path | None = None, timeout: float = 120
) -> dict[str, Any]:
    environment = environment_for(step, read_json(step.candidate))
    completed = _run(
        [str(_source_python(step.source)), "-X", "utf8", "-B", "-c", code, *args],
        cwd=step.source,
        environment=environment,
        timeout=timeout,
        evidence=evidence.with_suffix(".txt") if evidence else None,
    )
    value = json.loads(completed.stdout) if completed.stdout.strip() else {}
    if not isinstance(value, dict):
        raise ControlError("frozen validation must return a JSON object")
    if evidence is not None:
        # These fixed validation snippets emit identities and hashes, never credentials.
        write_report(evidence, value)
    return value


def preparation_argv(step: Step, *, setup_only: bool = False) -> list[str]:
    return [
        str(ROOT / ".venv/Scripts/python.exe"),
        "-X",
        "utf8",
        "-B",
        str(ROOT / "scripts/prepare_paired_control.py"),
        "--prepare-step",
        step.label,
        *(["--setup-only"] if setup_only else []),
    ]


def frozen_preflight(step: Step, evidence: Path) -> dict[str, Any]:
    return source_python(
        step,
        """
import importlib.metadata, json, sys
from pathlib import Path
import benchmarks, fulfillflow
from benchmarks.campaign import load_campaign
from benchmarks.dataset import verify_benchmark_artifacts
from benchmarks.host_probe import HostProbe
from benchmarks.run_campaign import _git_provenance, _project_release, _validate_prepare_command
root = Path.cwd()
bundle = load_campaign(Path(sys.argv[1]))
assert _git_provenance(root).sha == bundle.manifest.git_sha
assert _project_release(root) == bundle.manifest.release
assert importlib.metadata.version('fulfillflow') == bundle.manifest.release.removeprefix('v')
assert Path(sys.prefix).resolve() == (root / '.venv').resolve()
assert Path(benchmarks.__file__).resolve().is_relative_to(root)
assert Path(fulfillflow.__file__).resolve().is_relative_to(root)
assert verify_benchmark_artifacts(root / 'benchmarks/datasets') == bundle.dataset_sha256
_validate_prepare_command(json.loads(sys.argv[2]))
probe = HostProbe(bundle.manifest.host, official=False,
                  timeout_seconds=bundle.manifest.timeouts.command_seconds)
print(json.dumps({'valid': True, 'git_sha': bundle.manifest.git_sha,
 'dataset_sha256': bundle.dataset_sha256, 'python': sys.executable,
 'host_identity': probe.identity(), 'dynamic_admission': 'frozen runner after stabilization'}))
""",
        str(step.candidate),
        json.dumps(preparation_argv(step)),
        evidence=evidence,
    )


def fingerprints() -> dict[str, str]:
    paths = [
        *sorted((ROOT / "benchmarks").glob("*.py")),
        ROOT / "uv.lock",
        ROOT / "scripts/Invoke-PairedControls.ps1",
        ROOT / "scripts/prepare_paired_control.py",
        PILOT,
        AUDIT,
        *(step.candidate for step in STEPS),
    ]
    return {str(path.relative_to(ROOT)): _sha256(path) for path in paths}


def verify_candidates() -> None:
    originals = original_documents()
    for step in STEPS:
        if read_json(step.candidate) != candidate_document(step, originals):
            raise ControlError(f"{step.label} differs from its frozen authorized candidate")


def diagnostics(step: Step, destination: Path, *, stop: bool) -> None:
    environment = environment_for(step, read_json(step.candidate))
    destination.mkdir(exist_ok=False)
    failures = []
    if stop:
        try:
            _run(
                [*compose(step), "--profile", "campaign", "stop", "--timeout", "10", "loadgen"],
                cwd=step.source,
                environment=environment,
                evidence=destination / "stop-loadgen.txt",
            )
        except BaseException as exc:
            failures.append(error_report(exc))
    for filename, args in (
        ("containers.txt", ["ps", "--all", "--format", "json"]),
        ("logs.txt", ["logs", "--no-color", "--tail", "200"]),
    ):
        try:
            _run(
                [*compose(step), *args],
                cwd=step.source,
                environment=environment,
                evidence=destination / filename,
            )
        except BaseException as exc:
            failures.append(error_report(exc))
    # A stopped loadgen still retains the complete runtime for later manual copying.
    # On success, export it before removing infrastructure.
    if not stop:
        identifier = _run(
            [*compose(step), "ps", "--quiet", "loadgen"], cwd=step.source, environment=environment
        ).stdout.strip()
        if identifier:
            exists = _run(
                [
                    "docker",
                    "exec",
                    identifier,
                    "python",
                    "-c",
                    "from pathlib import Path; print(Path('/tmp/fulfillflow-benchmark').is_dir())",
                ],
                cwd=step.source,
                environment=environment,
            ).stdout.strip()
            if exists == "True":
                _run(
                    [
                        "docker",
                        "cp",
                        f"{identifier}:/tmp/fulfillflow-benchmark/.",
                        str(destination / "loadgen-runtime"),
                    ],
                    cwd=step.source,
                    environment=environment,
                )
    if failures:
        write_report(destination / "errors.json", failures)
        raise ControlError("diagnostic export or loadgen stop failed; resources preserved")


def cleanup(step: Step, evidence: Path) -> None:
    if read_json(evidence / "owned.json") != {
        "project": PROJECTS[step.version],
        "source": str(step.source),
    }:
        raise ControlError("cleanup ownership differs")
    environment = environment_for(step, read_json(step.candidate))
    _run(
        [
            *compose(step),
            "--profile",
            "campaign",
            "--profile",
            "preparation",
            "down",
            "--volumes",
            "--remove-orphans",
        ],
        cwd=step.source,
        environment=environment,
        evidence=evidence.parent / "cleanup.txt",
        timeout=30,
    )
    assert_projects_absent()


def prepare_step(step: Step, *, setup_only: bool) -> int:
    evidence = (PACKAGE / "setup" / step.version if setup_only else step.attempt) / "preparation"
    evidence.mkdir(parents=True, exist_ok=False)
    try:
        verify_source(step)
        verify_candidates()
        assert_projects_absent()
        document = read_json(step.candidate)
        environment = environment_for(step, document)
        image_preflight(step, document)
        _run(
            [*compose(step), "--profile", "campaign", "config", "--quiet"],
            cwd=step.source,
            environment=environment,
            evidence=evidence / "config.txt",
        )
        write_report(
            evidence / "owned.json", {"project": PROJECTS[step.version], "source": str(step.source)}
        )
        deadline = time.monotonic() + document["timeouts"]["preparation_seconds"]

        def command(args: list[str], name: str) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError("frozen preparation deadline exceeded")
            _run(
                [*compose(step), *args],
                cwd=step.source,
                environment=environment,
                timeout=remaining,
                evidence=evidence / f"{name}.txt",
            )

        if step.version == "v10":
            command(
                [
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
                "up",
            )
            command(
                [
                    "run",
                    "--rm",
                    "--no-deps",
                    "--pull",
                    "never",
                    "--volume",
                    f"{step.source}:/workspace:ro",
                    "--workdir",
                    "/workspace",
                    "app",
                    "python",
                    "-B",
                    "-m",
                    "benchmarks.prepare_database",
                    "--internal-seed",
                    "--confirm-database-name",
                    "fulfillflow_benchmark",
                ],
                "seed",
            )
        else:
            command(
                [
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "90",
                    "--no-build",
                    "--pull",
                    "never",
                    "core",
                    "tracking",
                ],
                "up",
            )
            command(["run", "--rm", "--no-deps", "--pull", "never", "prepare"], "seed")
            command(
                [
                    "run",
                    "--rm",
                    "--no-deps",
                    "--pull",
                    "never",
                    "prepare",
                    "--confirm-core-database",
                    "fulfillflow_core",
                    "--confirm-tracking-database",
                    "fulfillflow_tracking",
                    "--verify-only",
                ],
                "seed-verification",
            )
            command(
                [
                    "--profile",
                    "campaign",
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "30",
                    "--no-build",
                    "--pull",
                    "never",
                    "loadgen",
                ],
                "loadgen-idle",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError("frozen preparation deadline exceeded")
        verification = source_python(
            step,
            """
import json, sys
from pathlib import Path
from benchmarks.campaign import load_campaign
from benchmarks.collectors import DockerProbe, DatabaseProbe
from benchmarks.run_campaign import _verify_initial_state
bundle = load_campaign(Path(sys.argv[1]))
observed = DockerProbe(bundle, Path.cwd()).observe()
if bundle.manifest.release == 'v1.1.0':
    from benchmarks.collectors_v11 import SplitDatabaseProbe
    database = SplitDatabaseProbe(observed.container_ids['postgres'])
else:
    database = DatabaseProbe(observed.container_ids['postgres'], observed.postgres_user,
                             observed.postgres_database, timeout_seconds=30)
_verify_initial_state(bundle, database)
print(json.dumps({'verified': True, 'dataset_sha256': bundle.dataset_sha256,
                  'container_ids': observed.container_ids, 'load_executed': False}))
""",
            str(step.candidate),
            evidence=evidence / "verification.txt",
            timeout=remaining,
        )
        if time.monotonic() >= deadline:
            raise ControlError("verification exceeded frozen preparation deadline")
        write_report(evidence / "ready.json", verification)
        return 0
    except BaseException as exc:
        write_report(evidence / "error.json", error_report(exc))
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


def require_new_execution() -> None:
    if JOURNAL.exists() or any(step.attempt.exists() for step in STEPS):
        raise ControlError("execution destinations already exist; no retry or overwrite")


def prepared() -> None:
    require_new_execution()
    if (PACKAGE / "error.json").exists():
        raise ControlError("preparation failed; review required")
    expected = {
        path.relative_to(PACKAGE).as_posix(): _sha256(path)
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    }
    recorded = {}
    for line in (PACKAGE / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if name in recorded:
            raise ControlError("duplicate preparation checksum")
        recorded[name] = digest
    if expected != recorded:
        raise ControlError("preparation checksums differ; review required")
    ready = read_json(PACKAGE / "ready.json")
    if ready.get("fingerprints") != fingerprints() or ready.get("order") != [
        step.name for step in STEPS
    ]:
        raise ControlError("prepared block changed; review required")
    verify_candidates()
    for step in STEPS[:2]:
        verify_source(step)
        sync_source(step, check=True)


def prepare_package(launcher: Mapping[str, Any]) -> int:
    require_new_execution()
    if PACKAGE.exists() or any(step.source.exists() for step in STEPS[:2]):
        raise ControlError("preparation destinations must be new; no automatic retry")
    originals = original_documents()
    PACKAGE.mkdir()
    stage = "checkout"
    try:
        write_report(PACKAGE / "launcher.json", launcher)
        for step in STEPS[:2]:
            _run(
                ["git", "worktree", "add", "--detach", str(step.source), REVISIONS[step.version]],
                cwd=ROOT,
                timeout=60,
                evidence=PACKAGE / f"{step.version}-checkout.txt",
            )
            verify_source(step)
            stage = f"sync-{step.version}"
            sync_source(step)
            verify_source(step)
        for step in STEPS:
            write_report(step.candidate, candidate_document(step, originals))
        verify_candidates()
        stage = "read-only-preflight"
        assert_projects_absent()
        images = {}
        for step in STEPS:
            frozen_preflight(step, PACKAGE / f"{step.label}-preflight.json")
            images[step.label] = image_preflight(step, read_json(step.candidate))
        stage = "loadgen-audit"
        source_python(
            STEPS[1],
            """
import json, sys
from pathlib import Path
from benchmarks.prepare_v11 import reuse_loadgen_audit
print(json.dumps(reuse_loadgen_audit(sys.argv[1], Path(sys.argv[2]))))
""",
            originals["v11"]["images"]["loadgen"],
            str(AUDIT),
            evidence=PACKAGE / "loadgen-audit.json",
        )
        for step in STEPS[:2]:
            stage = f"setup-without-load-{step.version}"
            print(f"Verifying {step.version} database setup without load.", flush=True)
            setup = PACKAGE / "setup" / step.version
            setup.mkdir(parents=True)
            try:
                result = _run(
                    preparation_argv(step, setup_only=True),
                    cwd=ROOT,
                    timeout=120,
                    evidence=setup / "process.txt",
                    required=False,
                )
                if result.returncode:
                    raise ControlError(f"{step.version} setup failed; diagnostics: {setup}")
            except BaseException as exc:
                write_report(setup / "error.json", error_report(exc))
                if (setup / "preparation/owned.json").exists():
                    try:
                        diagnostics(step, setup / "diagnostics", stop=True)
                    except BaseException as diagnostic_exc:
                        write_report(setup / "diagnostic-error.json", error_report(diagnostic_exc))
                raise
            diagnostics(step, setup / "diagnostics", stop=False)
            cleanup(step, setup / "preparation")
        write_report(
            PACKAGE / "ready.json",
            {
                "order": [step.name for step in STEPS],
                "revisions": REVISIONS,
                "images": images,
                "fingerprints": fingerprints(),
                "load_executed": False,
                "database_setup_verified": ["v10", "v11"],
                "interpretation": {
                    "pairs": [["a1", "b1"], ["a2", "b2"]],
                    "rps_delta_percent": "100*(B/A-1)",
                    "p95_delta_ms": "B-A",
                    "all_four_must_be_valid": True,
                    "concordance": "both pairs: v11 lower throughput and higher p95",
                    "concordant_next_step": "propose official host/reference decision separately",
                    "otherwise": "inconclusive; diagnose before proposing more load",
                    "new_practical_margin": None,
                    "statistical_equivalence_claim": False,
                    "exclusive_causality_claim": False,
                },
            },
        )
        _write_checksums(PACKAGE)
        print(f"Prepared four controls without load: {PACKAGE / 'ready.json'}", flush=True)
        return 0
    except BaseException as exc:
        write_report(PACKAGE / "error.json", {"stage": stage, **error_report(exc)})
        _write_checksums(PACKAGE)
        raise ControlError(
            f"preparation stopped at {stage}; see {PACKAGE / 'error.json'}"
        ) from None


def run_step(step: Step, launcher: Mapping[str, Any]) -> int:
    step.attempt.mkdir()
    report: dict[str, Any] = {
        "complete": False,
        "step": step.name,
        "block": step.block,
        "label": step.label,
        "version": step.version,
        "launcher": dict(launcher),
    }
    code = 2
    try:
        verify_source(step)
        verify_candidates()
        assert_projects_absent()
        document = read_json(step.candidate)
        report["images"] = image_preflight(step, document)
        frozen_preflight(step, step.attempt / "preflight.json")
        write_report(step.attempt / "candidate.json", document)
        argv = preparation_argv(step)
        write_report(step.attempt / "preparation-argv.json", argv)
        completed = _run_runner(
            [
                str(_source_python(step.source)),
                "-X",
                "utf8",
                "-B",
                "-m",
                "benchmarks.run_campaign",
                "--manifest",
                str(step.candidate),
                "--execute",
                "--confirm-campaign",
                step.name,
                "--base-url",
                "http://app:8000" if step.version == "v10" else "http://core:8000",
                "--results-directory",
                str(step.attempt / "run"),
                "--prepare-command-json",
                json.dumps(argv),
            ],
            step.source,
            environment_for(step, document),
            step.attempt / "runner.txt",
        )
        code = completed.returncode
        if code:
            raise ControlError("frozen runner refused or interrupted the control")
        metadata = read_json(step.attempt / "run/mixed-4-users-r01/metadata.json")
        if metadata.get("valid") is not True or list(
            (step.attempt / "run").rglob(".incomplete.json")
        ):
            raise ControlError("runner did not produce a valid complete repetition")
        report["complete"] = True
    except BaseException as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else (code or 2)
        report.update(error_report(exc))
    finally:
        report["exit_code"] = code
        write_report(step.attempt / "result.json", report)
        owned = step.attempt / "preparation/owned.json"
        if owned.exists():
            try:
                diagnostics(step, step.attempt / "diagnostics", stop=code != 0)
                if code == 0:
                    cleanup(step, owned.parent)
                else:
                    report["resources_preserved_for_review"] = True
            except BaseException as exc:
                report["diagnostic_or_cleanup_error"] = error_report(exc)
                report["complete"] = False
                report["resources_preserved_for_review"] = True
                code = code or 2
        report["exit_code"] = code
        write_report(step.attempt / "result.json", report)
        _write_checksums(step.attempt)
    return code


def execute_block(launcher: Mapping[str, Any]) -> int:
    prepared()
    assert_projects_absent()
    JOURNAL.mkdir()
    report: dict[str, Any] = {
        "complete": False,
        "order": [step.name for step in STEPS],
        "completed": [],
        "launcher": dict(launcher),
        "package_ready_sha256": _sha256(PACKAGE / "ready.json"),
    }
    try:
        for step in STEPS:
            report["current"] = step.name
            write_report(JOURNAL / "result.json", report)
            print(
                f"Starting {step.name}; frozen timed phases take at least 11 minutes.", flush=True
            )
            code = run_step(step, launcher)
            if code:
                report["exit_code"] = code
                print(
                    f"Stopped without retry; diagnostics: {step.attempt / 'result.json'}",
                    file=sys.stderr,
                )
                return code
            report["completed"].append(step.name)
            print(f"Completed {step.name}.", flush=True)
        report.update(complete=True, exit_code=0)
        return 0
    except BaseException as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else 2
        report.update(error_report(exc), exit_code=code)
        return code
    finally:
        write_report(JOURNAL / "result.json", report)
        _write_checksums(JOURNAL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--prepare-step", choices=[step.label for step in STEPS])
    parser.add_argument("--setup-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.prepare_step:
            step = next(step for step in STEPS if step.label == args.prepare_step)
            if args.setup_only and step not in STEPS[:2]:
                raise ControlError("setup verification only uses the first step of each version")
            return prepare_step(step, setup_only=args.setup_only)
        if args.setup_only:
            raise ControlError("setup-only requires a preparation step")
        launcher = json.load(sys.stdin)
        if not isinstance(launcher, dict) or set(launcher) != {"executable", "version"}:
            raise ControlError("PowerShell identity is required")
        if args.plan_only:
            print(
                json.dumps(
                    {
                        "order": [step.name for step in STEPS],
                        "revisions": REVISIONS,
                        "package": str(PACKAGE),
                        "load_executed": False,
                        "readiness_verified": False,
                    }
                )
            )
            return 0
        return prepare_package(launcher) if args.prepare_only else execute_block(launcher)
    except BaseException as exc:
        print(json.dumps(error_report(exc)), file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())

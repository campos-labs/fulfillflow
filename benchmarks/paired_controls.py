"""Prepare frozen applications for explicitly selected, manually executed controls."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from benchmarks.campaign import load_campaign
from benchmarks.collectors import (
    DatabaseProbe,
    DockerProbe,
    ResourceSampler,
    validate_resource_samples,
)
from benchmarks.collectors_v11 import SplitDatabaseProbe, aggregate_resources
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
from benchmarks.logging_audit import audit_image
from benchmarks.operational_errors import error_report, write_report
from benchmarks.run_campaign import runner_provenance

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/results"
PACKAGE = RESULTS / "paired-win9445-review-01"
JOURNAL = RESULTS / "paired-win9445-execution-01"
SERIES = "historical"
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
    profile: str = "mixed"
    users: int = 4

    @property
    def name(self) -> str:
        if SERIES != "historical":
            return (
                f"reviewed-{SERIES}-{self.profile}-{self.users}-win9445-01-"
                f"{self.label}-{self.version}"
            )
        return f"paired-mixed-4-win9445-{self.number:02d}-{self.label}-{self.version}"

    @property
    def source(self) -> Path:
        if SERIES != "historical":
            return RESULTS / f"comparison-win9445-{self.version}-source-01"
        return RESULTS / f"paired-win9445-{self.version}-source-01"

    @property
    def candidate(self) -> Path:
        return PACKAGE / "candidates" / f"{self.name}.json"

    @property
    def attempt(self) -> Path:
        return RESULTS / self.name


STEPS: tuple[Step, ...] = (
    Step(1, 1, "v10", "a1"),
    Step(2, 1, "v11", "b1"),
    Step(3, 2, "v11", "b2"),
    Step(4, 2, "v10", "a2"),
)


def configure_series(series: str) -> None:
    """Select one explicit fixed campaign before any filesystem or Docker operation."""
    global SERIES, PACKAGE, JOURNAL, PROJECTS, STEPS
    if series == "historical":
        return
    if series not in {"abba", "official", "warmup"} or SERIES != "historical":
        raise ControlError("control series cannot be changed during execution")
    SERIES = series
    PACKAGE = RESULTS / f"reviewed-{series}-win9445-review-01"
    JOURNAL = RESULTS / f"reviewed-{series}-win9445-execution-01"
    PROJECTS = {
        "v10": "fulfillflow-task08-prepare-e2e-reviewed01",
        "v11": "fulfillflow-ii-reviewed-win9445",
    }
    if series == "warmup":
        PROJECTS = {"v11": "fulfillflow-ii-warmup12-win9445-01"}
        STEPS = (Step(1, 1, "v11", "w1", "mixed", 12),)
    if series == "official":
        steps: list[Step] = []
        for block, (profile, users) in enumerate(
            (
                ("mixed", 4),
                ("mixed", 12),
                ("timeline", 4),
                ("timeline", 12),
                ("ingestion", 4),
                ("ingestion", 12),
            ),
            1,
        ):
            order: tuple[Version, Version] = ("v10", "v11") if block % 2 else ("v11", "v10")
            for version in order:
                steps.append(
                    Step(len(steps) + 1, block, version, f"c{block}-{version}", profile, users)
                )
        STEPS = tuple(steps)


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
    originals = {"v10": _baseline_document(ROOT), "v11": pilot}
    if SERIES != "historical":
        for profile in ("timeline", "ingestion"):
            originals[f"v10-{profile}"] = json.loads(
                _git(ROOT, "show", f"v1.0.0:benchmarks/campaigns/v1-baseline-{profile}.json")
            )
    return originals


def candidate_document(step: Step, originals: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(json.dumps(originals[step.version])))
    if SERIES != "historical":
        reference = originals["v10" if step.profile == "mixed" else f"v10-{step.profile}"]
        if step.version == "v10":
            document = cast(dict[str, Any], json.loads(json.dumps(reference)))
        else:
            for field in ("profile", "weights", "loads"):
                document[field] = json.loads(json.dumps(reference[field]))
        document["environment"]["compose_project"] = PROJECTS[step.version]
    document.update(
        name=step.name, official=SERIES == "official", repetitions=5 if SERIES == "official" else 1
    )
    document["loads"] = [load for load in document["loads"] if load["users"] == step.users]
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
        *(["--offline"] if SERIES != "historical" and not check else []),
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
    if SERIES != "historical":
        environment["BENCH_APP_PORT"] = (
            "18039" if SERIES == "warmup" else "18037" if step.version == "v10" else "18038"
        )
    return _source_environment(step.source, environment)


def runner_environment(step: Step, document: Mapping[str, Any]) -> dict[str, str]:
    environment = environment_for(step, document)
    return _source_environment(ROOT, environment) if SERIES != "historical" else environment


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
        *(["--series", SERIES] if SERIES != "historical" else []),
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
        *([ROOT / "scripts/Invoke-ReviewedControls.ps1"] if SERIES != "historical" else []),
        *([ROOT / "scripts/Invoke-V11WarmupDiagnostic.ps1"] if SERIES == "warmup" else []),
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
    # docker cp also works after stopping; failures must retain their runtime too.
    if not stop or SERIES != "historical":
        identifier = _run(
            [*compose(step), "ps", "--all", "--quiet", "loadgen"],
            cwd=step.source,
            environment=environment,
        ).stdout.strip()
        if identifier:
            exists = (
                "True"
                if stop
                else _run(
                    [
                        "docker",
                        "exec",
                        identifier,
                        "python",
                        "-c",
                        "from pathlib import Path; "
                        "print(Path('/tmp/fulfillflow-benchmark').is_dir())",
                    ],
                    cwd=step.source,
                    environment=environment,
                ).stdout.strip()
            )
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
    owner_index: Path | None = None
    if SERIES == "official" and not setup_only:
        evidence.mkdir(parents=True, exist_ok=True)
        previous = sorted(evidence.glob("r[0-9][0-9]"))
        if len(previous) >= 5:
            raise ControlError("all five preparation destinations already exist")
        if previous:
            completed = step.attempt / f"run/{step.profile}-{step.users}-users-r{len(previous):02d}"
            if read_json(completed / "metadata.json").get("valid") is not True:
                raise ControlError("previous repetition is not valid; preparation cannot retry")
            diagnostics(step, previous[-1] / "diagnostics", stop=False)
            cleanup(step, previous[-1])
        owner_index = evidence / "owned.json"
        evidence = evidence / f"r{len(previous) + 1:02d}"
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
        if owner_index is not None and not owner_index.exists():
            write_report(
                owner_index, {"project": PROJECTS[step.version], "source": str(step.source)}
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


def verify_checksums(directory: Path) -> None:
    expected = {
        path.relative_to(directory).as_posix(): _sha256(path)
        for path in directory.rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    }
    recorded = {}
    for line in (directory / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if name in recorded:
            raise ControlError("duplicate evidence checksum")
        recorded[name] = digest
    if expected != recorded:
        raise ControlError("evidence checksums differ; review required")


def prepared() -> None:
    require_new_execution()
    if (PACKAGE / "error.json").exists():
        raise ControlError("preparation failed; review required")
    verify_checksums(PACKAGE)
    ready = read_json(PACKAGE / "ready.json")
    if ready.get("fingerprints") != fingerprints() or ready.get("order") != [
        step.name for step in STEPS
    ]:
        raise ControlError("prepared block changed; review required")
    if SERIES != "historical" and ready.get("host_runner") != runner_provenance(ROOT):
        raise ControlError("reviewed host runner differs from the prepared identity")
    if SERIES == "warmup" and ready.get("coordinator") != coordinator_identity():
        raise ControlError("warm-up coordinator differs from the prepared identity")
    verify_candidates()
    for step in STEPS[:2]:
        verify_source(step)
        sync_source(step, check=True)


def idle_verification(step: Step, destination: Path) -> None:
    """One fixed 300-second diagnostic per topology; never invokes Locust phases."""
    destination.mkdir(parents=True, exist_ok=False)
    bundle = load_campaign(step.candidate)
    observed = DockerProbe(bundle, step.source).observe()
    database = (
        SplitDatabaseProbe(observed.container_ids["postgres"])
        if step.version == "v11"
        else DatabaseProbe(
            observed.container_ids["postgres"], observed.postgres_user, observed.postgres_database
        )
    )
    sampler = ResourceSampler(
        destination / "resources.csv", observed.container_ids, database, 1, phase="idle"
    )
    started = time.monotonic()
    sampler.start()
    try:
        sampler.failed.wait(300)
    finally:
        sampler.stop()
    validate_resource_samples(sampler.output_path, observed.container_ids)
    if step.version == "v11":
        aggregate_resources(
            sampler.output_path, destination / "resources.application.csv", observed.container_ids
        )
    with sampler.output_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    write_report(
        destination / "summary.json",
        {
            "load_executed": False,
            "business_requests_sent": 0,
            "expected_seconds": 300,
            "observed_seconds": time.monotonic() - started,
            "cycles": len(rows) // len(observed.container_ids),
            "resource_rows": len(rows),
            "original_failure_cause_established": False,
            "under_load_stability_established": False,
        },
    )
    print(
        f"Completed idle collector verification for {step.version}: {len(rows)} rows.", flush=True
    )


def prepare_package(launcher: Mapping[str, Any]) -> int:
    require_new_execution()
    if PACKAGE.exists() or (
        SERIES not in {"official", "warmup"} and any(step.source.exists() for step in STEPS[:2])
    ):
        raise ControlError("preparation destinations must be new; no automatic retry")
    originals = original_documents()
    PACKAGE.mkdir()
    stage = "checkout"
    try:
        write_report(PACKAGE / "launcher.json", launcher)
        for step in STEPS[:2]:
            if SERIES in {"official", "warmup"}:
                verify_source(step)
                sync_source(step, check=True)
                continue
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
        if SERIES != "historical":
            logging_report: dict[str, Any] = {}
            for step in STEPS[:2]:
                role = "app" if step.version == "v10" else "core"
                logging_report[step.version] = audit_image(images[step.label][role]["identity"])
                config = json.loads(
                    _run(
                        [*compose(step), "--profile", "campaign", "config", "--format", "json"],
                        cwd=step.source,
                        environment=environment_for(step, read_json(step.candidate)),
                    ).stdout
                )
                logging_report[step.version]["compose"] = {}
                for service in ("app",) if step.version == "v10" else ("core", "tracking"):
                    effective = config["services"][service]
                    command = effective["command"]
                    if any(
                        option in command
                        for option in ("--log-level", "--log-config", "--no-access-log")
                    ):
                        raise ControlError("Compose overrides frozen Uvicorn logging defaults")
                    logging_report[step.version]["compose"][service] = {
                        "command": command,
                        "environment": {
                            key: effective["environment"].get(key)
                            for key in (
                                "LOG_LEVEL",
                                "LOG_FORMAT",
                                "OTEL_ENABLED",
                                "METRICS_ENABLED",
                            )
                        },
                    }
            logging_report["current_docker_logging_driver"] = _run(
                ["docker", "info", "--format", "{{.LoggingDriver}}"], cwd=ROOT
            ).stdout.strip()
            logging_report["historical_docker_output_confirmed"] = False
            write_report(PACKAGE / "logging-policy.json", logging_report)
        stage = "loadgen-audit"
        source_python(
            next(step for step in STEPS if step.version == "v11"),
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
                if SERIES == "abba":
                    print(
                        f"Checking reviewed {step.version} collector for 300 seconds without load.",
                        flush=True,
                    )
                    environment = runner_environment(step, read_json(step.candidate))
                    _run(
                        [
                            str(ROOT / ".venv/Scripts/python.exe"),
                            "-X",
                            "utf8",
                            "-B",
                            "-c",
                            "from pathlib import Path; "
                            "from benchmarks import paired_controls as c; "
                            "import sys; c.configure_series('abba'); "
                            "step=next(s for s in c.STEPS if s.label==sys.argv[1]); "
                            "c.idle_verification(step,Path(sys.argv[2]))",
                            step.label,
                            str(setup / "collector-idle"),
                        ],
                        cwd=ROOT,
                        environment=environment,
                        timeout=400,
                        evidence=setup / "collector-idle-process.txt",
                    )
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
                "database_setup_verified": [step.version for step in STEPS[:2]],
                "host_runner": runner_provenance(ROOT) if SERIES != "historical" else None,
                "coordinator": coordinator_identity() if SERIES == "warmup" else None,
                "series": SERIES,
                "repetitions_per_step": 5 if SERIES == "official" else 1,
                "official_execution_requires_abba_review": SERIES == "official",
                "interpretation": warmup_interpretation()
                if SERIES == "warmup"
                else {
                    "reference": "new current-host v1.0 image controls",
                    "repetitions_per_version": 30,
                    "aggregation": "medians and descriptive dispersion per cell",
                    "order_randomized": False,
                    "within_cell_order_effect_excluded": False,
                    "statistical_equivalence_claim": False,
                    "exclusive_causality_claim": False,
                }
                if SERIES == "official"
                else {
                    "pairs": [["a1", "b1"], ["a2", "b2"]],
                    "rps_delta_percent": "100*(B/A-1)",
                    "p95_delta_ms": "B-A",
                    "all_four_must_be_valid": True,
                    "concordance": "both pairs: v11 lower throughput and higher p95",
                    "concordant_next_step": (
                        "review results before manual current-host matrix"
                        if SERIES != "historical"
                        else "propose official host/reference decision separately"
                    ),
                    "otherwise": "inconclusive; diagnose before proposing more load",
                    "new_practical_margin": None,
                    "statistical_equivalence_claim": False,
                    "exclusive_causality_claim": False,
                },
            },
        )
        _write_checksums(PACKAGE)
        print(
            f"Prepared {len(STEPS)} campaign steps without load: {PACKAGE / 'ready.json'}",
            flush=True,
        )
        return 0
    except BaseException as exc:
        write_report(PACKAGE / "error.json", {"stage": stage, **error_report(exc)})
        _write_checksums(PACKAGE)
        raise ControlError(
            f"preparation stopped at {stage}; see {PACKAGE / 'error.json'}"
        ) from None


def coordinator_identity() -> dict[str, Any]:
    return {
        "git_sha": _git(ROOT, "rev-parse", "HEAD"),
        "components": {
            name: _sha256(ROOT / name)
            for name in (
                "benchmarks/paired_controls.py",
                "scripts/prepare_paired_control.py",
                "scripts/Invoke-V11WarmupDiagnostic.ps1",
            )
        },
    }


def warmup_interpretation() -> dict[str, Any]:
    return {
        "mode": "diagnostic_warmup_only",
        "official": False,
        "matrix_eligible": False,
        "attempts": 1,
        "measurement_executed": False,
        "equivalent_failure": "keep 12-user cells blocked; propose a decision before further load",
        "success": (
            "preserve previous failure and review divergence; no stability claim or matrix resume"
        ),
        "proven_defect": "correct within authorization and assess identities and evidence impact",
        "new_practical_margin": None,
        "automatic_retry": False,
    }


def verify_warmup_result(directory: Path) -> bool:
    verify_checksums(directory)
    metadata = read_json(directory / "warmup-diagnostic/metadata.json")
    return (
        metadata.get("mode") == "diagnostic_warmup_only"
        and metadata.get("diagnostic_complete") is True
        and metadata.get("warmup_valid") is True
        and metadata.get("valid") is False
        and metadata.get("official") is False
        and metadata.get("matrix_eligible") is False
        and metadata.get("measurement_executed") is False
    )


def run_step(step: Step, launcher: Mapping[str, Any]) -> int:
    step.attempt.mkdir()
    report: dict[str, Any] = {
        "complete": False,
        "stage": "source-and-candidate-validation",
        "step": step.name,
        "block": step.block,
        "label": step.label,
        "version": step.version,
        "launcher": dict(launcher),
        "mode": "diagnostic_warmup_only" if SERIES == "warmup" else "campaign",
        "coordinator": coordinator_identity() if SERIES == "warmup" else None,
    }
    code = 2
    try:
        verify_source(step)
        verify_candidates()
        assert_projects_absent()
        document = read_json(step.candidate)
        report["stage"] = "image-and-host-preflight"
        report["images"] = image_preflight(step, document)
        frozen_preflight(step, step.attempt / "preflight.json")
        write_report(step.attempt / "candidate.json", document)
        argv = preparation_argv(step)
        write_report(step.attempt / "preparation-argv.json", argv)
        report["stage"] = "runner"
        completed = _run_runner(
            [
                str(ROOT / ".venv/Scripts/python.exe")
                if SERIES != "historical"
                else str(_source_python(step.source)),
                "-X",
                "utf8",
                "-B",
                "-m",
                "benchmarks.run_campaign",
                *(["--application-source", str(step.source)] if SERIES != "historical" else []),
                "--manifest",
                str(step.candidate),
                "--execute",
                *(["--diagnostic-warmup-only"] if SERIES == "warmup" else []),
                "--confirm-campaign",
                step.name,
                "--base-url",
                "http://app:8000" if step.version == "v10" else "http://core:8000",
                "--results-directory",
                str(step.attempt / "run"),
                "--prepare-command-json",
                json.dumps(argv),
            ],
            ROOT if SERIES != "historical" else step.source,
            runner_environment(step, document),
            step.attempt / "runner.txt",
        )
        code = completed.returncode
        if code:
            phase_errors = sorted((step.attempt / "run").rglob("phase-error.json"))
            if phase_errors:
                error_path = phase_errors[-1]
                details = read_json(error_path)
                report["phase_diagnostic"] = str(error_path)
                stage = details.get("stage", "unknown")
                print(
                    f"Runner failed at {stage}, phase {details.get('phase', 'unknown')}, "
                    f"process exit {details.get('process_returncode')}; diagnostics: {error_path}",
                    file=sys.stderr,
                )
            raise ControlError(
                "reviewed runner refused or interrupted the control"
                if SERIES != "historical"
                else "frozen runner refused or interrupted the control"
            )
        expected_repetitions = 5 if SERIES == "official" else 1
        valid = (
            verify_warmup_result(step.attempt / "run")
            if SERIES == "warmup"
            else all(
                read_json(
                    step.attempt
                    / f"run/{step.profile}-{step.users}-users-r{number:02d}/metadata.json"
                ).get("valid")
                is True
                for number in range(1, expected_repetitions + 1)
            )
        )
        if not valid or list((step.attempt / "run").rglob(".incomplete.json")):
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


def abba_review() -> dict[str, Any]:
    """Require the completed new ABBA and apply its fixed descriptive decision rule."""
    journal = RESULTS / "reviewed-abba-win9445-execution-01/result.json"
    if not journal.is_file() or read_json(journal).get("complete") is not True:
        raise ControlError("new ABBA must complete before the official matrix can execute")
    package = RESULTS / "reviewed-abba-win9445-review-01"
    verify_checksums(journal.parent)
    verify_checksums(package)
    ready = read_json(package / "ready.json")
    execution = read_json(journal)
    expected_order = [
        f"reviewed-abba-mixed-4-win9445-01-{label}-{version}"
        for label, version in (("a1", "v10"), ("b1", "v11"), ("b2", "v11"), ("a2", "v10"))
    ]
    if (
        execution.get("exit_code") != 0
        or execution.get("order") != expected_order
        or execution.get("completed") != expected_order
        or execution.get("package_ready_sha256") != _sha256(package / "ready.json")
        or ready.get("order") != expected_order
        or ready.get("host_runner") != runner_provenance(ROOT)
    ):
        raise ControlError("ABBA journal, package or host runner identity differs")
    rows: dict[str, dict[str, float]] = {}
    inputs = {}
    for label, version in (("a1", "v10"), ("b1", "v11"), ("b2", "v11"), ("a2", "v10")):
        attempt = RESULTS / f"reviewed-abba-mixed-4-win9445-01-{label}-{version}"
        if read_json(attempt / "result.json").get("complete") is not True:
            raise ControlError("ABBA attempt is incomplete")
        verify_checksums(attempt)
        repetition = attempt / "run/mixed-4-users-r01"
        metadata = read_json(repetition / "metadata.json")
        if metadata.get("valid") is not True or list(attempt.rglob(".incomplete.json")):
            raise ControlError("ABBA repetition failed its gates")
        candidate = package / f"candidates/{attempt.name}.json"
        if (
            metadata.get("campaign") != attempt.name
            or metadata.get("git", {}).get("sha") != REVISIONS[version]
            or metadata.get("host_runner") != ready["host_runner"]
            or metadata.get("manifest_sha256") != _sha256(candidate)
            or read_json(attempt / "candidate.json") != read_json(candidate)
        ):
            raise ControlError("ABBA application, manifest or runner identity differs")
        with (repetition / "locust_stats.csv").open(newline="", encoding="utf-8-sig") as stream:
            aggregate = next(row for row in csv.DictReader(stream) if row["Name"] == "Aggregated")
        rows[label] = {
            "requests_per_second": float(aggregate["Requests/s"]),
            "p95_ms": float(aggregate["95%"]),
        }
        if not all(math.isfinite(value) and value > 0 for value in rows[label].values()):
            raise ControlError("ABBA metrics must be finite and positive")
        inputs[label] = _sha256(attempt / "checksums.sha256")
    contrasts = []
    for a, b in (("a1", "b1"), ("a2", "b2")):
        contrasts.append(
            {
                "rps_delta_percent": 100
                * (rows[b]["requests_per_second"] / rows[a]["requests_per_second"] - 1),
                "p95_delta_ms": rows[b]["p95_ms"] - rows[a]["p95_ms"],
            }
        )
    return {
        "rows": rows,
        "contrasts": contrasts,
        "input_checksums": inputs,
        "concordant": all(
            row["rps_delta_percent"] < 0 and row["p95_delta_ms"] > 0 for row in contrasts
        ),
        "statistical_equivalence_claim": False,
    }


def execute_block(launcher: Mapping[str, Any]) -> int:
    review = None
    if SERIES == "official":
        review = abba_review()
        if not review["concordant"]:
            raise ControlError("ABBA contrasts are inconclusive; review before more load")
    prepared()
    if SERIES == "warmup" and read_json(PACKAGE / "launcher.json") != dict(launcher):
        raise ControlError("PowerShell identity differs from the prepared warm-up launcher")
    assert_projects_absent()
    JOURNAL.mkdir()
    report: dict[str, Any] = {
        "complete": False,
        "order": [step.name for step in STEPS],
        "completed": [],
        "launcher": dict(launcher),
        "package_ready_sha256": _sha256(PACKAGE / "ready.json"),
        "abba_review": review,
    }
    try:
        for step in STEPS:
            report["current"] = step.name
            write_report(JOURNAL / "result.json", report)
            print(
                f"Starting {step.name}; frozen timed phases take at least "
                f"{55 if SERIES == 'official' else 6 if SERIES == 'warmup' else 11} minutes"
                f"{' (warm-up only; no measurement)' if SERIES == 'warmup' else ''}.",
                flush=True,
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
    parser.add_argument(
        "--series", choices=["historical", "abba", "official", "warmup"], default="historical"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--prepare-step")
    parser.add_argument("--setup-only", action="store_true")
    args = parser.parse_args()
    try:
        configure_series(args.series)
        if args.prepare_step:
            step = next((step for step in STEPS if step.label == args.prepare_step), None)
            if step is None:
                raise ControlError("preparation step is not part of the selected series")
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
                        "mode": "diagnostic_warmup_only" if SERIES == "warmup" else "campaign",
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

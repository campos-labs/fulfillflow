"""Explicit 59-measurement continuation; immutable external r01, never implicit resume."""

import argparse
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from benchmarks import comparison_controls as comparison
from benchmarks import paired_controls as controls
from benchmarks.comparison_protocol import PROTOCOL, load_comparison_campaign
from benchmarks.controls_v10 import ControlError, _git, _run, _sha256, _write_checksums
from benchmarks.host_probe import HostProbe
from benchmarks.operational_errors import error_report, write_report
from benchmarks.run_campaign import runner_provenance
from benchmarks.sensitivity_controls import PWSH

ROOT = comparison.ROOT
RESULTS = ROOT / "benchmarks/results"
ORIGINAL = RESULTS / "reviewed-comparison120-win9445-review-01"
ORIGINAL_RELEASE = RESULTS / "comparison120-win9445-release-01"
ORIGINAL_ATTEMPT = RESULTS / "reviewed-comparison120-mixed-4-win9445-01-c1-v10-v10"
PRESERVED = ORIGINAL_ATTEMPT / "run/mixed-4-users-r01"
PACKAGE = RESULTS / "comparison120-continuation-review-02"
RELEASE = RESULTS / "comparison120-continuation-release-02"
HISTORY = {
    "reviewed-comparison120-win9445-review-01": (
        "76be09f071f979e5711178a5544d60e876bd8cbf0d3d01369edb8e4c28a95123"
    ),
    "comparison120-win9445-release-01": (
        "3ecf3e87d123d7f7476e047e7cdb4a2e238cbb59fd95008d7e56e613789b2461"
    ),
    "reviewed-comparison120-win9445-execution-01": (
        "6bd0e084ed66f1c545bbed91a80c04f3fc31f943ef6fda8bb689ce70792a2963"
    ),
    "reviewed-comparison120-mixed-4-win9445-01-c1-v10-v10": (
        "5116adc6090b7a33da5046a0b0181c9d58592da99d29fd2dc763b689deb0c56b"
    ),
}
CONTRACT = "comparison120-explicit-continuation-v1"


def configure() -> None:
    controls.configure_series("comparison120")
    controls.PACKAGE = PACKAGE
    controls.JOURNAL = RESULTS / "comparison120-continuation-execution-02"
    controls.PROJECTS = {
        version: f"fulfillflow-comparison120-continuation-01-{version}"
        for version in ("v10", "v11")
    }
    controls.STEPS = tuple(replace(step, continuation=True) for step in controls.STEPS)
    comparison.RELEASE = RELEASE


def coordinator_identity() -> dict[str, Any]:
    return {
        "git_sha": _git(ROOT, "rev-parse", "HEAD"),
        "components": {
            name: _sha256(ROOT / name)
            for name in (
                "benchmarks/comparison_continuation.py",
                "benchmarks/comparison_controls.py",
                "benchmarks/paired_controls.py",
                "scripts/Invoke-Comparison120Continuation.ps1",
            )
        },
    }


def inventory() -> list[dict[str, Any]]:
    return [
        {
            "campaign": step.name,
            "destination": str(step.attempt),
            "repetitions": list(range(step.first_repetition, 6)),
            "manifest": str(comparison.executable(step)),
        }
        for step in controls.STEPS
    ]


def verify_history() -> dict[str, Any]:
    for name, digest in HISTORY.items():
        path = RESULTS / name
        if _sha256(path / "checksums.sha256") != digest:
            raise ControlError("preserved campaign identity changed")
        controls.verify_checksums(path)
    ready = controls.read_json(ORIGINAL_RELEASE / "ready.json")
    if ready["ci_commit"] != "02fe942b597f2e85e1bd2df5b9a3be6507561257":
        raise ControlError("preserved runner differs")
    first = controls.STEPS[0]
    comparison.verify_repetition(
        first,
        PRESERVED,
        recorded_runner=ready["runner"],
        manifest_path=ORIGINAL / "candidates" / comparison.executable(first).name,
    )
    return {
        "path": str(PRESERVED),
        "checksums_sha256": _sha256(PRESERVED / "checksums.sha256"),
        "runner": ready["runner"],
        "history": HISTORY,
    }


def verify_review() -> dict[str, Any]:
    draft = comparison.verify_draft()
    if (
        draft.get("continuation_contract") != CONTRACT
        or draft.get("inventory") != inventory()
        or draft.get("preserved") != verify_history()
        or draft.get("coordinator", {}).get("components") != coordinator_identity()["components"]
    ):
        raise ControlError("continuation plan or preserved identity changed")
    # Projects alone differ; workload manifests cannot acquire a second protocol delta.
    for step in controls.STEPS:
        for current in (step.candidate, comparison.executable(step)):
            old = controls.read_json(ORIGINAL / "candidates" / current.name)
            old["environment"]["compose_project"] = controls.PROJECTS[step.version]
            if controls.read_json(current) != old:
                raise ControlError("continuation manifest differs beyond isolated project")
    return draft


def verify_release() -> dict[str, Any]:
    verify_review()
    return comparison.verify_release()


def seal_execution(ci_run: str, approval_reference: str) -> None:
    verify_review()
    comparison.seal_execution(ci_run, approval_reference)
    verify_release()


def runner_segment(manifest: Path, results: Path) -> list[Path]:
    if controls.SERIES == "historical":
        configure()
    verify_release()
    matches = [
        step
        for step in controls.STEPS
        if comparison.executable(step).resolve() == manifest.resolve()
        and (step.attempt / "run").resolve() == results.resolve()
    ]
    if len(matches) != 1 or not matches[0].continuation:
        raise ControlError("runner segment destination or manifest differs")
    return [PRESERVED] if matches[0].number == 1 else []


def verify_segment(step: controls.Step) -> None:
    verify_history()
    prefix = [PRESERVED] if step.number == 1 else []
    expected = {
        "schema_version": 1,
        "preserved": [
            {"path": str(path), "checksums_sha256": _sha256(path / "checksums.sha256")}
            for path in prefix
        ],
        "first_repetition": step.first_repetition,
        "host_runner": runner_provenance(ROOT),
    }
    if controls.read_json(step.attempt / "run/continuation.json") != expected:
        raise ControlError("segment reference or runner provenance differs")


def original_resources() -> tuple[list[str], dict[str, str], set[str]]:
    step = controls.STEPS[0]
    controls.verify_source(step)
    project = "fulfillflow-comparison120-win9445-01-v10"
    owned = controls.read_json(ORIGINAL_ATTEMPT / "preparation/owned.json")
    if owned != {"project": project, "source": str(step.source)}:
        raise ControlError("original cleanup ownership differs")
    # The sealed Compose inventory includes exited migration containers as well as runtime roles.
    inventory_path = ORIGINAL_ATTEMPT / "diagnostics/containers.txt"
    expected = {
        json.loads(line)["ID"]
        for line in inventory_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    actual = set(
        _run(
            [
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            cwd=ROOT,
        ).stdout.split()
    )
    if actual and (
        len(actual) != len(expected)
        or not all(
            sum(identifier.startswith(prefix) for prefix in expected) == 1 for identifier in actual
        )
    ):
        raise ControlError("original project containers differ; preserve for investigation")
    argv = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        str(step.source / "compose.benchmark.yaml"),
        "--file",
        str(ORIGINAL / "loadgen-v10.json"),
    ]
    document = controls.read_json(ORIGINAL / "candidates" / step.candidate.name)
    return argv, controls.environment_for(step, document), actual


def retire_original_resources() -> None:
    """Manual execution only: export to the new journal, then remove proven owned resources."""
    verify_history()
    argv, environment, actual = original_resources()
    if actual:
        _run(
            [*argv, "logs", "--no-color", "--tail", "200"],
            cwd=ROOT,
            environment=environment,
            evidence=controls.JOURNAL / "previous-project-logs.txt",
        )
        _run(
            [
                *argv,
                "--profile",
                "campaign",
                "--profile",
                "preparation",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=ROOT,
            environment=environment,
            evidence=controls.JOURNAL / "previous-project-cleanup.txt",
            timeout=30,
        )
    for command in (
        ["docker", "ps", "--all", "--quiet"],
        ["docker", "volume", "ls", "--quiet"],
        ["docker", "network", "ls", "--quiet"],
    ):
        if _run(
            [
                *command,
                "--filter",
                "label=com.docker.compose.project=fulfillflow-comparison120-win9445-01-v10",
            ],
            cwd=ROOT,
        ).stdout.strip():
            raise ControlError("previous resources remain; no preparation may begin")


def prepare_review(launcher: dict[str, Any]) -> None:
    if _git(ROOT, "branch", "--show-current") != "codex/v1.1-tracking":
        raise ControlError("continuation requires authorized branch")
    if Path(launcher.get("executable", "")).resolve() != PWSH.resolve() or not PWSH.is_file():
        raise ControlError("use the verified PowerShell executable")
    controls.require_new_execution()
    if PACKAGE.exists() or RELEASE.exists():
        raise ControlError("continuation package already exists; no overwrite")
    preserved = verify_history()
    controls.assert_projects_absent()
    # Observe the host identity without removing the previous owned stack in preparation.
    old_manifest = load_comparison_campaign(
        ORIGINAL / "candidates" / comparison.executable(controls.STEPS[0]).name
    )
    probe = HostProbe(old_manifest.manifest.host, official=True, timeout_seconds=30)
    identity = probe.identity()
    _, _, old_resources = original_resources()
    PACKAGE.mkdir()
    for name in ("images.json", "loadgen-v10.json", "loadgen-v11.json"):
        shutil.copyfile(ORIGINAL / name, PACKAGE / name)
    for step in controls.STEPS:
        for path in (step.candidate, comparison.executable(step)):
            document = controls.read_json(ORIGINAL / "candidates" / path.name)
            document["environment"]["compose_project"] = controls.PROJECTS[step.version]
            write_report(path, document)
    comparison.verify_candidates()
    image_report = {}
    for step in controls.STEPS[:2]:
        controls.verify_source(step)
        controls.sync_source(step, check=True)
        image_report[step.version] = controls.image_preflight(
            step, controls.read_json(step.candidate)
        )
        _run(
            [*controls.compose(step), "--profile", "campaign", "config", "--quiet"],
            cwd=step.source,
            environment=controls.environment_for(step, controls.read_json(step.candidate)),
            evidence=PACKAGE / f"compose-{step.version}.txt",
        )
    write_report(
        PACKAGE / "review.json",
        {
            "protocol": PROTOCOL,
            "continuation_contract": CONTRACT,
            "state": "awaiting-independent-review-before-final-commit",
            "execution_released": False,
            "runner": runner_provenance(ROOT, require_clean=False),
            "inputs": comparison.inputs(),
            "launcher": launcher,
            "order": [step.name for step in controls.STEPS],
            "inventory": inventory(),
            "preserved": preserved,
            "coordinator": coordinator_identity(),
            "host_identity": identity,
            "images": image_report,
            "original_owned_containers_pending_manual_cleanup": len(old_resources),
            "new_measurements": 59,
            "timed_floor_seconds": 42480,
            "backup_confirmed": False,
            "load_executed": False,
        },
    )
    _write_checksums(PACKAGE)
    verify_review()


def execute(launcher: dict[str, Any]) -> int:
    verify_release()
    if launcher != controls.read_json(PACKAGE / "review.json")["launcher"]:
        raise ControlError("PowerShell identity differs")
    controls.require_new_execution()
    controls.assert_projects_absent()
    controls.JOURNAL.mkdir()
    report: dict[str, Any] = {
        "complete": False,
        "coordinator": coordinator_identity(),
        "completed": [],
        "continuation_contract": CONTRACT,
        "preserved": verify_history(),
        "package_sha256": _sha256(PACKAGE / "checksums.sha256"),
    }
    code = 2
    try:
        # Verify stable host identity before even the owned cleanup.
        manifest = load_comparison_campaign(comparison.executable(controls.STEPS[0]))
        probe = HostProbe(manifest.manifest.host, official=True, timeout_seconds=30)
        report["host_identity"] = probe.identity()
        retire_original_resources()
        report["host_conditions"] = probe.dynamic({})
        for step in controls.STEPS:
            verify_release()
            report["current"] = step.name
            write_report(controls.JOURNAL / "result.json", report)
            print(
                f"Starting continuation {step.name}: r{step.first_repetition:02d}-r05.", flush=True
            )
            code = controls.run_step(step, launcher)
            if code:
                return code
            controls.verify_checksums(step.attempt)
            comparison.verify_block(step)
            report["completed"].append(step.name)
        report["complete"] = True
        return 0
    except BaseException as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else 2
        report.update(error_report(exc))
        return code
    finally:
        report["exit_code"] = 0 if report["complete"] else code
        write_report(controls.JOURNAL / "result.json", report)
        _write_checksums(controls.JOURNAL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare-review", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--prepare-step")
    args = parser.parse_args()
    try:
        configure()
        if args.prepare_step:
            verify_release()
            step = next(s for s in controls.STEPS if s.label == args.prepare_step)
            return controls.prepare_step(step, setup_only=False)
        launcher = json.load(sys.stdin)
        if args.prepare_review:
            prepare_review(launcher)
            return 0
        return execute(launcher)
    except BaseException as exc:
        print(json.dumps(error_report(exc)), file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Reviewable symmetric comparison; execution requires a separately sealed clean commit."""

import argparse
import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks import paired_controls as controls
from benchmarks.campaign import HostContract
from benchmarks.comparison_images import derive
from benchmarks.comparison_protocol import (
    PROTOCOL,
    load_comparison_campaign,
    verify_warmup_progress,
)
from benchmarks.controls_v10 import ControlError, _git, _run, _sha256, _write_checksums
from benchmarks.host_probe import HostProbe
from benchmarks.operational_errors import error_report, write_report
from benchmarks.run_campaign import runner_provenance
from benchmarks.sensitivity_controls import PWSH

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "benchmarks/results/comparison120-win9445-release-01"


def configure() -> None:
    controls.configure_series("comparison120")


def executable(step: controls.Step) -> Path:
    return step.candidate.with_name(step.candidate.stem + "-execution.json")


def inputs() -> dict[str, str]:
    paths = [
        *ROOT.glob("benchmarks/*.py"),
        *ROOT.glob("src/**/*.py"),
        *ROOT.glob("tests/unit/test_*benchmark*.py"),
        *ROOT.glob("tests/unit/test_*comparison*.py"),
        ROOT / "scripts/Invoke-Comparison120.ps1",
        ROOT / "scripts/Invoke-Comparison120Continuation.ps1",
        ROOT / "scripts/prepare_paired_control.py",
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        ROOT / "README.md",
        ROOT / "DESIGN.md",
        ROOT / "RELEASE_PLAN.md",
        ROOT / "benchmarks/README.md",
        ROOT / "benchmarks/V11_REVIEW.md",
    ]
    return {p.relative_to(ROOT).as_posix(): _sha256(p) for p in sorted(set(paths))}


def candidate_execution(step: controls.Step) -> dict[str, Any]:
    result = copy.deepcopy(controls.read_json(step.candidate))
    result.update(protocol=PROTOCOL, warmup_seconds=120)
    result["timeouts"]["warmup_process_seconds"] += 60
    return result


def verify_candidates() -> None:
    controls.verify_candidates()
    for step in controls.STEPS:
        if controls.read_json(executable(step)) != candidate_execution(step):
            raise ControlError("comparison manifest differs from its declared policy delta")
        load_comparison_campaign(executable(step))


def verify_draft() -> dict[str, Any]:
    controls.verify_checksums(controls.PACKAGE)
    draft = controls.read_json(controls.PACKAGE / "review.json")
    if draft["inputs"] != inputs() or draft["protocol"] != PROTOCOL:
        raise ControlError("reviewed comparison source or protocol changed")
    if draft["order"] != [step.name for step in controls.STEPS]:
        raise ControlError("reviewed comparison order changed")
    verify_candidates()
    return draft


def verify_release() -> dict[str, Any]:
    draft = verify_draft()
    if not RELEASE.is_dir():
        raise ControlError("execution awaits independent review, final commit and its CI")
    controls.verify_checksums(RELEASE)
    ready = controls.read_json(RELEASE / "ready.json")
    if (
        ready.get("review_approved") is not True
        or ready.get("ci_verified") is not True
        or ready.get("package_sha256") != _sha256(controls.PACKAGE / "checksums.sha256")
        or ready.get("runner") != runner_provenance(ROOT)
        or ready.get("ci_commit") != _git(ROOT, "rev-parse", "HEAD")
        or _git(ROOT, "branch", "--show-current") != "codex/v1.1-tracking"
    ):
        raise ControlError("independent review, final clean commit and its CI are required")
    if ready["runner"]["components"] != draft["runner"]["components"]:
        raise ControlError("final runner components differ from reviewed files")
    return ready


def seal_execution(ci_run: str, approval_reference: str) -> None:
    """Called only after the user's independent-review release and the final commit CI."""
    draft = verify_draft()
    if not ci_run.isdecimal() or not approval_reference.strip():
        raise ControlError("explicit review authorization and CI run are required")
    if RELEASE.exists():
        raise ControlError("execution release already exists; no replacement")
    provenance = runner_provenance(ROOT)
    if _git(ROOT, "branch", "--show-current") != "codex/v1.1-tracking":
        raise ControlError("final release requires authorized branch")
    _git(ROOT, "ls-files", "--error-unmatch", *inputs())
    if provenance["components"] != draft["runner"]["components"]:
        raise ControlError("committed runner differs from reviewed sources")
    ci = json.loads(
        _run(
            ["gh", "run", "view", ci_run, "--json", "headSha,headBranch,status,conclusion,url"],
            cwd=ROOT,
        ).stdout
    )
    if (
        ci.get("headSha") != _git(ROOT, "rev-parse", "HEAD")
        or ci.get("headBranch") != "codex/v1.1-tracking"
        or ci.get("status") != "completed"
        or ci.get("conclusion") != "success"
    ):
        raise ControlError("CI must succeed for the exact final comparison commit")
    RELEASE.mkdir()
    write_report(
        RELEASE / "ready.json",
        {
            "review_approved": True,
            "approval_reference": approval_reference,
            "ci_verified": True,
            "ci_commit": ci["headSha"],
            "ci": ci,
            "package_sha256": _sha256(controls.PACKAGE / "checksums.sha256"),
            "runner": provenance,
            "load_executed": False,
        },
    )
    _write_checksums(RELEASE)


def verify_repetition(
    step: controls.Step,
    directory: Path,
    *,
    recorded_runner: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
) -> None:
    controls.verify_checksums(directory)
    metadata = controls.read_json(directory / "metadata.json")
    manifest_path = manifest_path or executable(step)
    bundle = load_comparison_campaign(manifest_path)
    if (
        metadata.get("valid") is not True
        or metadata.get("official") is not True
        or metadata.get("campaign") != step.name
        or metadata.get("git", {}).get("sha") != controls.REVISIONS[step.version]
        or metadata.get("host_runner") != (recorded_runner or runner_provenance(ROOT))
        or metadata.get("manifest_sha256") != _sha256(manifest_path)
        or directory.name
        != f"{step.profile}-{step.users}-users-r{metadata.get('repetition', 0):02d}"
        or metadata.get("protocol_expected")
        != bundle.manifest.model_dump(mode="json", exclude={"loads"})
        or metadata.get("load") != bundle.manifest.loads[0].model_dump(mode="json")
        or metadata.get("warmup", {}).get("exit_code") != 0
        or metadata.get("measurement", {}).get("exit_code") != 0
        or list(directory.rglob(".incomplete.json"))
    ):
        raise ControlError("comparison repetition did not pass identity and completion gates")
    verify_warmup_progress(directory / "warmup", step.users)
    if step.version == "v11":
        reconciliation = controls.read_json(directory / "reconciliation.json")
        if len(set(reconciliation[k] for k in ("commands", "receipts", "finalized"))) != 1:
            raise ControlError("comparison owner reconciliation differs")


def verify_block(step: controls.Step) -> None:
    run = step.attempt / "run"
    expected = {
        f"{step.profile}-{step.users}-users-r{n:02d}" for n in range(step.first_repetition, 6)
    }
    if {p.name for p in run.iterdir() if p.is_dir()} != expected:
        raise ControlError("comparison requires exactly five complete repetitions")
    if step.continuation:
        from benchmarks.comparison_continuation import verify_segment

        verify_segment(step)
    for name in sorted(expected):
        verify_repetition(step, run / name)
    if not (run / "summary.csv").is_file() or list(run.rglob(".incomplete.json")):
        raise ControlError("comparison block summary is incomplete")


def prepare_review(launcher: dict[str, Any]) -> None:
    if _git(ROOT, "branch", "--show-current") != "codex/v1.1-tracking":
        raise ControlError("comparison preparation requires the authorized branch")
    if Path(launcher.get("executable", "")).resolve() != PWSH.resolve() or not PWSH.is_file():
        raise ControlError("use the verified PowerShell executable")
    controls.require_new_execution()
    if controls.PACKAGE.exists() or RELEASE.exists():
        raise ControlError("comparison review destination already exists")
    # Admission evidence is separate, including when Docker is unavailable before package creation.
    admission = (
        ROOT
        / "benchmarks/results/comparison120-preparation-admission"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    admission.mkdir(parents=True, exist_ok=False)
    _run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        cwd=ROOT,
        evidence=admission / "docker-version.txt",
    )
    originals = controls.original_documents()
    host = HostContract.model_validate(originals["v11"]["host"])
    probe = HostProbe(host, official=True, timeout_seconds=30)
    write_report(
        admission / "host.json", {"identity": probe.identity(), "conditions": probe.dynamic({})}
    )
    _write_checksums(admission)
    controls.assert_projects_absent()
    for step in controls.STEPS[:2]:
        controls.verify_source(step)
        controls.sync_source(step, check=True)
    historical = ROOT / "benchmarks/results/warmup-sensitivity-win9445-review-01"
    controls.verify_checksums(historical)
    controls.PACKAGE.mkdir()
    images = {
        version: derive(ROOT, controls.PACKAGE / "images" / version, version, audit["image"])
        for version, audit in controls.read_json(historical / "images.json").items()
    }
    write_report(controls.PACKAGE / "images.json", images)
    for version, audit in images.items():
        write_report(
            controls.PACKAGE / f"loadgen-{version}.json",
            {"services": {"loadgen": {"image": audit["image"]}}},
        )
    for step in controls.STEPS:
        write_report(step.candidate, controls.candidate_document(step, originals))
        write_report(executable(step), candidate_execution(step))
    verify_candidates()
    for step in controls.STEPS[:2]:
        controls.image_preflight(step, controls.read_json(step.candidate))
        controls.frozen_preflight(step, controls.PACKAGE / f"preflight-{step.version}.json")
        bundle = load_comparison_campaign(executable(step))
        HostProbe(bundle.manifest.host, official=True, timeout_seconds=30).dynamic({})
        _run(
            [*controls.compose(step), "--profile", "campaign", "config", "--quiet"],
            cwd=step.source,
            environment=controls.environment_for(step, controls.read_json(step.candidate)),
            evidence=controls.PACKAGE / f"compose-{step.version}.txt",
        )
    write_report(
        controls.PACKAGE / "review.json",
        {
            "protocol": PROTOCOL,
            "state": "awaiting-independent-review-before-final-commit",
            "execution_released": False,
            "runner": runner_provenance(ROOT, require_clean=False),
            "inputs": inputs(),
            "launcher": launcher,
            "order": [s.name for s in controls.STEPS],
            "destinations": [str(s.attempt) for s in controls.STEPS],
            "repetitions_per_block": 5,
            "timed_floor_seconds": 43200,
            "original_sensitivity_package_sha256": _sha256(historical / "checksums.sha256"),
            "backup_confirmed": False,
            "load_executed": False,
            "admission_evidence": str(admission),
            "admission_sha256": _sha256(admission / "checksums.sha256"),
        },
    )
    _write_checksums(controls.PACKAGE)


def execute(launcher: dict[str, Any]) -> int:
    verify_release()
    if launcher != controls.read_json(controls.PACKAGE / "review.json")["launcher"]:
        raise ControlError("PowerShell identity differs")
    controls.require_new_execution()
    controls.assert_projects_absent()
    controls.JOURNAL.mkdir()
    report: dict[str, Any] = {
        "complete": False,
        "completed": [],
        "protocol": PROTOCOL,
        "package_sha256": _sha256(controls.PACKAGE / "checksums.sha256"),
    }
    code = 2
    try:
        for step in controls.STEPS:
            verify_release()
            report["current"] = step.name
            write_report(controls.JOURNAL / "result.json", report)
            print(f"Starting {step.name}; five repetitions, at least 60 timed minutes.", flush=True)
            code = controls.run_step(step, launcher)
            if code:
                print(
                    f"Stopped without retry; diagnostics: {step.attempt / 'result.json'}",
                    flush=True,
                )
                return code
            controls.verify_checksums(step.attempt)
            verify_block(step)
            report["completed"].append(step.name)
            print(f"Completed and verified {step.name}.", flush=True)
        report["complete"] = True
        code = 0
        return code
    except BaseException as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else 2
        report.update(error_report(exc))
        return code
    finally:
        report["exit_code"] = code
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

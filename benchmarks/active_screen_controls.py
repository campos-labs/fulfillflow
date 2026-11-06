"""Prepare and supervise one nonofficial five-repetition active-screen block."""

import argparse
import copy
import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks import paired_controls as controls
from benchmarks.active_screen_energy import require_energy
from benchmarks.active_screen_protocol import PROTOCOL, load_active_screen_campaign
from benchmarks.comparison_protocol import verify_warmup_progress
from benchmarks.controls_v10 import ControlError, _git, _run, _run_runner, _sha256, _write_checksums
from benchmarks.host_probe import HostProbe
from benchmarks.operational_errors import error_report, sanitize, write_report
from benchmarks.run_campaign import ACTIVE_SCREEN_RUNNER_MODE, runner_provenance
from benchmarks.sensitivity_images import inventory as image_inventory

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/results"
PACKAGE = RESULTS / "active-screen-mixed4-review-09"
RELEASE = RESULTS / "active-screen-release-02"
IDLE = RESULTS / "active-screen-idle-02"
PARENT = "sha256:543c5756796ac4173aa57771ac846c060aa68ae1872184cc0bf42afb76b29431"
ORIGINAL = RESULTS / "comparison120-continuation-review-02"
PROJECT = "fulfillflow-active-screen-mixed4-01-v11"


def query_power_settings() -> str:
    """Decode native output using the inherited console, never replacement characters."""
    if sys.platform != "win32":
        raise ControlError("native power identity requires Windows")
    codepage = ctypes.windll.kernel32.GetConsoleOutputCP()
    if not codepage:
        raise ControlError("power query requires an identifiable console output code page")
    result = subprocess.run(
        ["powercfg", "/query"], capture_output=True, cwd=ROOT, timeout=30, check=False
    )
    guard = os.environ.get("FULFILLFLOW_ACTIVE_GUARD")
    evidence = {"codepage": codepage, "exit_code": result.returncode}
    if result.returncode:
        raise ControlError(f"power query exited {result.returncode}; code page {codepage}")
    try:
        output = result.stdout.decode(f"cp{codepage}").replace("\r\n", "\n")
    except UnicodeError as exc:
        raise ControlError(f"power query decoding failed; code page {codepage}") from exc
    if guard:
        write_report(
            Path(guard).parent / "power-query" / f"{uuid4()}.json",
            {**evidence, "settings": output},
        )
    return output


def coordinator_status(stage: str, load_executed: bool | None) -> None:
    guard = os.environ.get("FULFILLFLOW_ACTIVE_GUARD")
    if guard:
        write_report(
            Path(guard).parent / "coordinator-status.json",
            {"stage": stage, "load_executed": load_executed},
        )


def configure() -> controls.Step:
    controls.SERIES = "active-screen"
    controls.PACKAGE = PACKAGE
    controls.PROJECTS = {"v11": PROJECT}
    step = controls.Step(1, 1, "v11", "screen")
    controls.STEPS = (step,)
    return step


def executable() -> Path:
    return PACKAGE / "execution.json"


def inputs() -> dict[str, str]:
    paths = [
        *ROOT.glob("benchmarks/*.py"),
        *ROOT.glob("src/**/*.py"),
        ROOT / "scripts/ActiveScreenGuard.cs",
        ROOT / "scripts/ActiveScreenIO.cs",
        ROOT / "scripts/ActiveScreenIO.ps1",
        ROOT / "scripts/Invoke-ActiveScreenDiagnostic.ps1",
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        ROOT / "DESIGN.md",
        ROOT / "RELEASE_PLAN.md",
        ROOT / "benchmarks/README.md",
        ROOT / "benchmarks/V11_REVIEW.md",
        ROOT / "tests/unit/test_active_screen.py",
        ROOT / "tests/unit/test_active_screen_io.py",
    ]
    return {p.relative_to(ROOT).as_posix(): _sha256(p) for p in sorted(paths)}


def documents(image: str) -> tuple[dict[str, Any], dict[str, Any]]:
    original = ORIGINAL / "candidates/reviewed-comparison120-mixed-4-win9445-01-c1-v11-v11.json"
    preparation = copy.deepcopy(controls.read_json(original))
    dataset = Path(preparation["cohorts"]["dataset_manifest"])
    if not dataset.is_absolute():
        preparation["cohorts"]["dataset_manifest"] = str((original.parent / dataset).resolve())
    preparation.update(name=configure().name, official=False)
    preparation["environment"]["compose_project"] = PROJECT
    preparation["images"]["loadgen"] = image
    execution = copy.deepcopy(preparation)
    execution.update(protocol=PROTOCOL, matrix_eligible=False, warmup_seconds=120)
    execution["timeouts"]["warmup_process_seconds"] = 150
    return preparation, execution


def verify_candidates() -> None:
    step = configure()
    audit = controls.read_json(PACKAGE / "image/audit.json")
    preparation, execution = documents(audit["image"])
    if (
        controls.read_json(step.candidate) != preparation
        or controls.read_json(executable()) != execution
    ):
        raise ControlError("active-screen candidate differs from its declared delta")
    load_active_screen_campaign(executable())


def verify_package() -> dict[str, Any]:
    controls.verify_checksums(PACKAGE)
    review = controls.read_json(PACKAGE / "review.json")
    if review["inputs"] != inputs():
        raise ControlError("reviewed active-screen files changed")
    verify_candidates()
    return review


def require_release() -> None:
    review = verify_package()
    ready = controls.read_json(RELEASE / "ready.json")
    controls.verify_checksums(RELEASE)
    idle = controls.read_json(IDLE / "result.json")
    if (
        ready.get("package_sha256") != _sha256(PACKAGE / "checksums.sha256")
        or ready.get("approved") is not True
        or ready.get("isolation_review_approved") is not True
        or ready.get("git_sha") != _git(ROOT, "rev-parse", "HEAD")
        or ready.get("ci_head_sha") != ready.get("git_sha")
        or ready.get("ci_conclusion") != "success"
        or ready.get("inputs") != review["inputs"]
        or idle.get("exit_code") != 0
        or idle.get("released") is not True
        or idle.get("mode") != "IdleCheck"
        or idle.get("duration_seconds", 0) < 960
        or idle.get("load_executed") is not False
        or idle.get("guard_sha256") != review["inputs"]["scripts/ActiveScreenGuard.cs"]
        or idle.get("launcher_sha256") != review["idle_validation"]["launcher_sha256"]
        or ready.get("idle_result_sha256") != _sha256(IDLE / "result.json")
    ):
        raise ControlError("explicit review release and successful 960-second idle check required")
    runner_provenance(ROOT, require_clean=True, diagnostic_mode=ACTIVE_SCREEN_RUNNER_MODE)
    current_power = query_power_settings()
    if current_power != review["power_settings"]:
        raise ControlError("persistent power settings differ from prepared identity")


def verify_repetition(directory: Path) -> None:
    require_energy()
    controls.verify_checksums(directory)
    m = controls.read_json(directory / "metadata.json")
    bundle = load_active_screen_campaign(executable())
    if (
        m.get("valid") is not True
        or m.get("official") is not False
        or m.get("matrix_eligible") is not False
        or m.get("mode") != PROTOCOL
        or m.get("git", {}).get("sha") != controls.REVISIONS["v11"]
        or m.get("host_runner")
        != runner_provenance(ROOT, diagnostic_mode=ACTIVE_SCREEN_RUNNER_MODE)
        or m.get("manifest_sha256") != _sha256(executable())
        or m.get("protocol_expected") != bundle.manifest.model_dump(mode="json", exclude={"loads"})
        or m.get("load") != bundle.manifest.loads[0].model_dump(mode="json")
        or m.get("warmup", {}).get("exit_code") != 0
        or m.get("measurement", {}).get("exit_code") != 0
        or list(directory.rglob(".incomplete.json"))
    ):
        raise ControlError("diagnostic repetition failed identity/completion gates")
    verify_warmup_progress(directory / "warmup", 4)
    r = controls.read_json(directory / "reconciliation.json")
    if len({r[k] for k in ("commands", "receipts", "finalized")}) != 1:
        raise ControlError("diagnostic owner reconciliation differs")


def capture(destination: Path) -> None:
    """Extra export only between repetitions or after failure; no periodic SQL additions."""
    step = configure()
    if controls.read_json(step.attempt / "preparation/owned.json") != {
        "project": PROJECT,
        "source": str(step.source),
    }:
        raise ControlError("diagnostic capture ownership differs")
    destination.mkdir(parents=True, exist_ok=False)
    env = controls.environment_for(step, controls.read_json(step.candidate))
    for name, args in (
        ("timestamped-logs.txt", ["logs", "--timestamps", "--no-color", "--tail", "10000"]),
        ("containers.json", ["ps", "--all", "--format", "json"]),
    ):
        _run(
            [*controls.compose(step), *args],
            cwd=step.source,
            environment=env,
            evidence=destination / name,
        )


def prepare_review() -> None:
    step = configure()
    if _git(ROOT, "branch", "--show-current") != "codex/v1.1-tracking":
        raise ControlError("authorized branch required")
    if PACKAGE.exists() or step.attempt.exists():
        raise ControlError("review or execution destination exists")
    controls.verify_checksums(ORIGINAL)
    PACKAGE.mkdir()
    image_dir = PACKAGE / "image"
    image_dir.mkdir()
    before = image_inventory(PARENT)
    tag = "fulfillflow-loadgen:active-screen-mixed4-02"
    if _run(["docker", "image", "ls", "--quiet", tag], cwd=ROOT).stdout.strip():
        raise ControlError("derived image tag exists")
    names = ("active_screen_protocol.py", "locustfile.py")
    for name in names:
        shutil.copyfile(ROOT / "benchmarks" / name, image_dir / name)
    # A local alias permits BuildKit to resolve the immutable, already verified parent offline.
    parent_tag = "fulfillflow-loadgen:active-screen-parent-02"
    if _run(["docker", "image", "ls", "--quiet", parent_tag], cwd=ROOT).stdout.strip():
        raise ControlError("parent alias already exists")
    _run(["docker", "tag", PARENT, parent_tag], cwd=ROOT)
    (image_dir / "Dockerfile").write_text(
        f'FROM {parent_tag}\nLABEL org.fulfillflow.parent="{PARENT}"\n'
        f"COPY {' '.join(names)} /work/benchmarks/\n",
        encoding="utf-8",
    )
    _run(
        ["docker", "build", "--pull=false", "--network=none", "--tag", tag, str(image_dir)],
        cwd=ROOT,
        timeout=180,
        evidence=image_dir / "build.txt",
    )
    image = _run(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], cwd=ROOT
    ).stdout.strip()
    after = image_inventory(image)
    changed = sorted(
        k
        for k in before["files"].keys() | after["files"].keys()
        if before["files"].get(k) != after["files"].get(k)
    )
    if changed != sorted(names) or before["packages"] != after["packages"]:
        raise ControlError("image differs beyond authorized contract/selection")
    write_report(
        image_dir / "audit.json",
        {
            "parent": PARENT,
            "image": image,
            "before": before,
            "after": after,
            "changed_files": changed,
            "dependencies_unchanged": True,
        },
    )
    preparation, execution = documents(image)
    step.candidate.parent.mkdir()
    write_report(step.candidate, preparation)
    write_report(executable(), execution)
    write_report(PACKAGE / "loadgen-v11.json", {"services": {"loadgen": {"image": image}}})
    verify_candidates()
    controls.verify_source(step)
    controls.image_preflight(step, preparation)
    host = HostProbe(
        load_active_screen_campaign(executable()).manifest.host, official=True, timeout_seconds=30
    )
    identity = host.identity()
    write_report(
        PACKAGE / "review.json",
        {
            "protocol": PROTOCOL,
            "official": False,
            "matrix_eligible": False,
            "inputs": inputs(),
            "host": identity,
            "power_settings": query_power_settings(),
            "runner": runner_provenance(
                ROOT, require_clean=False, diagnostic_mode=ACTIVE_SCREEN_RUNNER_MODE
            ),
            "application_sha": controls.REVISIONS["v11"],
            "destination": str(step.attempt),
            "repetitions": [f"mixed-4-users-r{n:02d}" for n in range(1, 6)],
            "manual_idle_check_pending": True,
            "idle_validation": {
                "launcher_sha256": inputs()["scripts/Invoke-ActiveScreenDiagnostic.ps1"]
            },
            "released": False,
            "load_executed": False,
        },
    )
    _write_checksums(PACKAGE)


def execute() -> int:
    coordinator_status("preflight", False)
    step = configure()
    require_release()
    require_energy()
    if step.attempt.exists():
        raise ControlError("attempt exists; no retry")
    controls.assert_projects_absent()
    # Gate all competing containers before creating any new owned runtime resources.
    if _run(["docker", "ps", "--quiet"], cwd=ROOT).stdout.strip():
        raise ControlError("running competing containers; manual isolation required")
    step.attempt.mkdir()
    report: dict[str, Any] = {"official": False, "matrix_eligible": False, "complete": False}
    code = 2
    try:
        document = controls.read_json(step.candidate)
        controls.frozen_preflight(step, step.attempt / "preflight.json")
        args = [
            str(ROOT / ".venv/Scripts/python.exe"),
            "-X",
            "utf8",
            "-B",
            "-m",
            "benchmarks.run_campaign",
            "--active-screen-diagnostic",
            "--execute",
            "--manifest",
            str(executable()),
            "--application-source",
            str(step.source),
            "--confirm-campaign",
            step.name,
            "--base-url",
            "http://core:8000",
            "--results-directory",
            str(step.attempt / "run"),
            "--prepare-command-json",
            json.dumps(controls.preparation_argv(step)),
        ]
        coordinator_status("runner_dispatched", None)
        result = _run_runner(
            args, ROOT, controls.runner_environment(step, document), step.attempt / "runner.txt"
        )
        write_report(
            step.attempt / "runner-process.json",
            {
                "exit_code": result.returncode,
                "stdout": sanitize(result.stdout),
                "stderr": sanitize(result.stderr),
            },
        )
        code = result.returncode
        if code:
            raise ControlError("diagnostic runner stopped; preserve partial evidence")
        for n in range(1, 6):
            verify_repetition(step.attempt / f"run/mixed-4-users-r{n:02d}")
        report["complete"] = True
        coordinator_status("completed", True)
    except BaseException as exc:
        code = 2
        report["primary"] = error_report(exc)
    finally:
        if (step.attempt / "preparation/owned.json").exists():
            try:
                capture(step.attempt / "final-capture")
            except BaseException as exc:
                code = 2
                report["capture_export"] = error_report(exc)
            try:
                controls.diagnostics(step, step.attempt / "diagnostics", stop=code != 0)
            except BaseException as exc:
                code = 2
                report["diagnostics"] = error_report(exc)
        report.update(exit_code=code, resources_preserved=True)
        if code:
            report["complete"] = False
        if not persist_result(step.attempt, report):
            code = 2
    return code


def persist_result(destination: Path, report: dict[str, Any]) -> bool:
    """Keep the primary diagnostic in stderr even if its file cannot be exported."""
    try:
        write_report(destination / "result.json", report)
        _write_checksums(destination)
        return True
    except Exception as exc:
        report.update(artifact_export=error_report(exc), complete=False, exit_code=2)
        print(json.dumps(report), file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare-review", action="store_true")
    modes.add_argument("--prepare-step", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        step = configure()
        if args.prepare_review:
            prepare_review()
            return 0
        if args.prepare_step:
            require_release()
            require_energy()
            return controls.prepare_step(step, setup_only=False)
        return execute()
    except Exception as exc:
        print(json.dumps(error_report(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

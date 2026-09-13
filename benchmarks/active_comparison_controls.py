"""New symmetric comparison under the explicitly revised active-host procedure."""

import argparse
import json
import os
import shutil
import sys
from dataclasses import replace

from benchmarks import comparison_controls as comparison
from benchmarks import paired_controls as controls
from benchmarks.active_screen_controls import query_power_settings
from benchmarks.active_screen_energy import require_energy
from benchmarks.comparison_isolation import isolate, prepare_isolation
from benchmarks.comparison_protocol import PROTOCOL
from benchmarks.controls_v10 import ControlError, _run, _sha256, _write_checksums
from benchmarks.operational_errors import error_report, write_report
from benchmarks.run_campaign import runner_provenance
from benchmarks.sensitivity_controls import PWSH

ROOT = comparison.ROOT
PACKAGE = ROOT / "benchmarks/results/comparison-active-review-02"
RELEASE = ROOT / "benchmarks/results/comparison-active-release-02"
OPERATION = ROOT / "benchmarks/results/comparison-active-operation-02"
PREVIOUS = ROOT / "benchmarks/results/reviewed-comparison120-win9445-review-01"


def configure() -> None:
    os.environ["FULFILLFLOW_COMPARISON_ACTIVE"] = "1"
    comparison.configure()
    controls.PACKAGE = PACKAGE
    controls.JOURNAL = ROOT / "benchmarks/results/comparison-active-execution-02"
    controls.PROJECTS = {v: f"fulfillflow-comparison-active-02-{v}" for v in ("v10", "v11")}
    controls.STEPS = tuple(replace(s, label="active02-" + s.label) for s in controls.STEPS)
    comparison.RELEASE = RELEASE


def verify_host_policy() -> None:
    require_energy()
    expected = controls.read_json(PACKAGE / "host-policy.json")
    if query_power_settings() != expected["power_settings"]:
        raise ControlError("persistent power settings differ from the prepared active policy")


def prepare() -> None:
    controls.require_new_execution()
    controls.assert_projects_absent()
    if PACKAGE.exists() or RELEASE.exists() or OPERATION.exists():
        raise ControlError("new comparison destinations must be absent")
    controls.verify_checksums(PREVIOUS)
    PACKAGE.mkdir()
    # Reuse preserved comparison loadgen images; no build or source overlay.
    for name in ("images.json", "loadgen-v10.json", "loadgen-v11.json"):
        shutil.copyfile(PREVIOUS / name, PACKAGE / name)
    originals = controls.original_documents()
    for step in controls.STEPS:
        write_report(step.candidate, controls.candidate_document(step, originals))
        write_report(comparison.executable(step), comparison.candidate_execution(step))
    comparison.verify_candidates()
    reference = controls.read_json(
        ROOT / "benchmarks/results/active-screen-mixed4-review-09/review.json"
    )
    power = query_power_settings()
    if power != reference["power_settings"]:
        raise ControlError("power policy differs from preserved active-screen identity")
    for step in controls.STEPS[:2]:
        controls.verify_source(step)
        controls.image_preflight(step, controls.read_json(step.candidate))
        controls.frozen_preflight(step, PACKAGE / f"preflight-{step.version}.json")
    write_report(
        PACKAGE / "host-policy.json",
        {
            "policy": "active-screen-v1",
            "power_settings": power,
            "idle_reference": "active-screen-idle-02",
            "idle_repeat_required": False,
            "advancement_condition_revised": True,
            "prior_diagnostic_complete": False,
            "previous_measurements_included": 0,
            "historical_integrity_wal": "unverified",
            "independent_backup_confirmed": False,
        },
    )
    write_report(
        PACKAGE / "review.json",
        {
            "protocol": PROTOCOL,
            "runner": runner_provenance(ROOT, require_clean=False),
            "inputs": comparison.inputs(),
            "order": [s.name for s in controls.STEPS],
            "destinations": [str(s.attempt) for s in controls.STEPS],
            "launcher": {"executable": str(PWSH)},
            "timed_floor_seconds": 43200,
            "repetitions_per_block": 5,
            "execution_released": False,
            "load_executed": False,
            "images_parent_package_sha256": _sha256(PREVIOUS / "checksums.sha256"),
        },
    )
    prepare_isolation(PACKAGE)
    _write_checksums(PACKAGE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--prepare-step")
    args = parser.parse_args()
    try:
        configure()
        if args.prepare:
            prepare()
            return 0
        if args.prepare_step:
            comparison.verify_release()
            step = next(s for s in controls.STEPS if s.label == args.prepare_step)
            return controls.prepare_step(step, setup_only=False)
        comparison.verify_release()
        controls.require_new_execution()
        controls.assert_projects_absent()
        isolate(PACKAGE, OPERATION / "isolation")
        require_energy()
        if _run(["docker", "ps", "--quiet"], cwd=ROOT).stdout.strip():
            raise ControlError("running competing containers; isolated manual shutdown required")
        return comparison.execute(controls.read_json(PACKAGE / "review.json")["launcher"])
    except Exception as exc:
        print(json.dumps(error_report(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

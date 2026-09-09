"""Operational entry for one explicitly requested, non-official mixed/4 attempt."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from benchmarks import run_campaign as runner
from benchmarks.campaign import CampaignManifest, load_campaign
from benchmarks.collectors import run_capture
from benchmarks.host_probe import HostProbe
from benchmarks.operational_errors import diagnostics, error_report, write_report
from benchmarks.prepare_v11 import (
    COMPOSE,
    PILOT_WINDOWS_NAME,
    ROOT,
    compose_prefix,
    pilot_windows_document,
    reuse_loadgen_audit,
)


def pilot_document(candidate: Path, destination: Path) -> dict[str, Any]:
    source = load_campaign(candidate)
    document = source.manifest.model_dump(mode="json")
    baseline = CampaignManifest.model_validate_json(
        (ROOT / "benchmarks/campaigns/v1-baseline-mixed.json").read_text()
    ).model_dump(mode="json")
    if document["release"] != "v1.1.0":
        raise ValueError("pilot requires a reviewed v1.1 candidate")
    if not document["official"]:
        if document["name"] != PILOT_WINDOWS_NAME:
            raise ValueError("unrecognized non-official pilot decision")
        baseline = pilot_windows_document(baseline)
    if document["internal_timeouts"] != {"core_seconds": 6, "tracking_seconds": 8}:
        raise ValueError("candidate differs from the reviewed internal deadlines")
    changed = {
        "name",
        "schema_version",
        "release",
        "git_sha",
        "resources",
        "pool",
        "images",
        "cohorts",
        "environment",
        "database",
        "internal_timeouts",
    }
    if any(document[key] != value for key, value in baseline.items() if key not in changed):
        raise ValueError("candidate differs from frozen mixed workload or comparison parameters")
    for role in ("postgres", "loadgen"):
        if document["resources"][role] != baseline["resources"][role]:
            raise ValueError("candidate differs from frozen component budgets")
    for key, value in baseline["cohorts"].items():
        if key != "dataset_manifest" and document["cohorts"][key] != value:
            raise ValueError("candidate differs from frozen dataset/cohorts")
    if document["environment"]["compose_file"] != COMPOSE.name:
        raise ValueError("candidate must use the reviewed owner Compose")
    compose_prefix(document["environment"]["compose_project"])
    if document["official"]:
        document.update(name="v11-pilot-mixed-4-q430", official=False, repetitions=1)
    document["loads"] = [load for load in document["loads"] if load["users"] == 4]
    if len(document["loads"]) != 1 or document["warmup_quota_per_shipment"] != 430:
        raise ValueError("pilot requires mixed/4 and q=430")
    dataset = (candidate.parent / source.manifest.cohorts.dataset_manifest).resolve()
    document["cohorts"]["dataset_manifest"] = os.path.relpath(dataset, destination).replace(
        "\\", "/"
    )
    return CampaignManifest.model_validate(document).model_dump(mode="json")


def preparation_argv(project: str, manifest: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "benchmarks.prepare_v11",
        "restore",
        "--project",
        project,
        "--confirm-project",
        project,
        "--with-loadgen",
        "--manifest",
        str(manifest),
    ]


def preflight(candidate: Path, audit: Path, document: dict[str, Any]) -> dict[str, object]:
    provenance = runner._git_provenance(ROOT)
    if (
        provenance.branch != "codex/v1.1-tracking"
        or provenance.sha != document["git_sha"]
        or not provenance.worktree_clean
        or not provenance.staged_clean
        or runner._project_release(ROOT) != document["release"]
    ):
        raise ValueError("pilot requires the clean reviewed branch/SHA and rebuilt images")
    sums = json.loads((candidate.parent / "SHA256SUMS.json").read_text(encoding="utf-8-sig"))
    if sums.get(candidate.name) != runner._file_sha256(candidate):
        raise ValueError("candidate checksum differs from the review package")
    project = document["environment"]["compose_project"]
    if (ROOT / "benchmarks/results/preparation" / f"{project}.json").exists():
        raise ValueError(
            "pilot refuses an existing project ownership marker; preserve/review it first"
        )
    for kind, argv in (
        ("containers", ["docker", "ps", "-aq"]),
        ("volumes", ["docker", "volume", "ls", "-q"]),
    ):
        if run_capture(
            [*argv, "--filter", f"label=com.docker.compose.project={project}"], 30
        ).stdout.strip():
            raise ValueError(f"pilot refuses preexisting project {kind}")
    for role, identifier in document["images"].items():
        info = json.loads(run_capture(["docker", "image", "inspect", identifier], 30).stdout)[0]
        identities = {info["Id"], *(v.rsplit("@", 1)[-1] for v in info.get("RepoDigests", []))}
        if identifier not in identities:
            raise ValueError(f"unavailable {role} image identity")
        if (
            role in {"core", "tracking"}
            and (info["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            != provenance.sha
        ):
            raise ValueError(f"{role} image revision differs from source")
    compatibility = reuse_loadgen_audit(document["images"]["loadgen"], audit)
    manifest = CampaignManifest.model_validate(document)
    host = HostProbe(
        manifest.host, official=False, timeout_seconds=manifest.timeouts.command_seconds
    )
    return {
        "host_identity": host.identity(),
        "host_state": host.dynamic({}),
        "loadgen": compatibility,
    }


def execute(candidate: Path, destination: Path, audit: Path, *, plan_only: bool) -> int:
    candidate, destination, audit = candidate.resolve(), destination.resolve(), audit.resolve()
    document = pilot_document(candidate, destination)
    project = document["environment"]["compose_project"]
    manifest = destination / "pilot-manifest.json"
    argv = preparation_argv(project, manifest)
    runner._validate_prepare_command(argv)
    if destination.exists():
        raise ValueError("destination must be new; no automatic retry or artifact overwrite")
    if plan_only:
        print(
            json.dumps(
                {
                    "manifest": document,
                    "prepare_argv": argv,
                    "results_directory": str(destination / "run"),
                    "load_executed": False,
                }
            )
        )
        return 0
    destination.mkdir(parents=True, exist_ok=False)
    write_report(manifest, document)
    write_report(destination / "preparation-argv.json", argv)
    code = 2
    report: dict[str, object] = {"complete": False, "stage": "preflight"}
    previous = {
        key: os.environ.get(key)
        for key in (
            "FULFILLFLOW_PREPARATION_EVIDENCE",
            "BENCH_CORE_IMAGE",
            "BENCH_TRACKING_IMAGE",
            "BENCH_LOADGEN_IMAGE",
        )
    }
    entered_runner = False
    try:
        write_report(destination / "preflight.json", preflight(candidate, audit, document))
        os.environ["FULFILLFLOW_PREPARATION_EVIDENCE"] = str(destination / "preparation")
        for role in ("core", "tracking", "loadgen"):
            os.environ[f"BENCH_{role.upper()}_IMAGE"] = document["images"][role]
        report["stage"] = "runner"
        entered_runner = True
        code = runner._execute(
            load_campaign(manifest), manifest, "http://core:8000", destination / "run", argv
        )
        report["complete"] = code == 0
    except BaseException as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else 2
        report.update(error_report(exc))
        preparation_error = destination / "preparation/error.json"
        if preparation_error.exists():
            report["preparation_error"] = json.loads(preparation_error.read_text())
    finally:
        # Persist the original failure before any diagnostic/cleanup operation can fail.
        report["exit_code"] = code
        write_report(destination / "result.json", report)
        try:
            if entered_runner:
                diagnostics(
                    compose_prefix(project), destination / "diagnostics", include_runtime=True
                )
                marker = ROOT / "benchmarks/results/preparation" / f"{project}.json"
                if marker.exists():
                    run_capture(
                        [
                            sys.executable,
                            "-B",
                            "-m",
                            "benchmarks.prepare_v11",
                            "cleanup",
                            "--project",
                            project,
                            "--confirm-project",
                            project,
                        ],
                        120,
                    )
        except BaseException as exc:
            report["diagnostic_or_cleanup_error"] = error_report(exc)
            code = code or (130 if isinstance(exc, KeyboardInterrupt) else 2)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        report["exit_code"] = code
        write_report(destination / "result.json", report)
        runner._write_checksums(destination)
    print(json.dumps(report, ensure_ascii=False))
    return code


def main() -> int:
    try:
        options = json.load(sys.stdin)
        if set(options) != {"candidate", "destination", "audit", "plan_only"} or not isinstance(
            options["plan_only"], bool
        ):
            raise ValueError("invalid operational options")
        return execute(
            Path(options["candidate"]),
            Path(options["destination"]),
            Path(options["audit"]),
            plan_only=options["plan_only"],
        )
    except BaseException as exc:
        print(json.dumps(error_report(exc)), file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())

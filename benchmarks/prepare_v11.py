"""Restore an owned v1.1 project or materialize observed candidate manifests; never run load."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from benchmarks.campaign import CampaignManifest, load_campaign
from benchmarks.collectors import DatabaseProbe, DockerProbe, ExternalCommandError, run_capture
from benchmarks.collectors_v11 import SplitDatabaseProbe
from benchmarks.database_contract import STRUCTURAL_SCHEMA_QUERIES, structural_schema_identity
from benchmarks.operational_errors import diagnostics, error_report, write_report
from benchmarks.prepare_database import _validate_database_url
from benchmarks.seed_v11 import OWNER_SCHEMA_TABLES, Owner

ROOT = Path(__file__).resolve().parents[1]
FROZEN_LOADGEN = "sha256:f5b7118626bc3cf156029b9d31399bcba78013a035835db16815616c46bdd906"
FROZEN_TAG = "fulfillflow-loadgen:v10-frozen-f5b7118626bc"
COMPOSE = ROOT / "compose.benchmark-v11.yaml"


def compose_prefix(project: str) -> list[str]:
    if not re.fullmatch(r"fulfillflow-(?:benchmark-v11|ii-[a-z0-9-]+)", project):
        raise ValueError("project must be the v1.1 benchmark or an isolated increment II project")
    return ["docker", "compose", "-p", project, "-f", str(COMPOSE)]


def restore(
    project: str,
    confirmation: str,
    *,
    cleanup: bool = False,
    manifest: Path | None = None,
    with_loadgen: bool = False,
) -> None:
    """Delete only a project previously claimed while absent, then verify both owners."""
    started = time.monotonic()
    compose = compose_prefix(project)
    if confirmation != project:
        raise ValueError("literal project confirmation is required")
    bundle = load_campaign(manifest.resolve()) if manifest is not None else None
    if bundle is not None and (
        bundle.manifest.release != "v1.1.0"
        or bundle.manifest.environment.compose_project != project
        or bundle.manifest.environment.compose_file != COMPOSE.name
    ):
        raise ValueError("manifest must identify this v1.1 project and Compose contract")
    for owner in ("core", "tracking"):
        _validate_database_url(
            os.environ.get(f"{owner.upper()}_DATABASE_URL", ""),
            f"fulfillflow_{owner}",
            expected_user=f"fulfillflow_{owner}",
            expected_password=os.environ.get(f"{owner.upper()}_DB_PASSWORD"),
        )
    marker = ROOT / "benchmarks/results/preparation" / f"{project}.json"
    readiness = marker.with_suffix(".ready.json")
    identity = {"project": project, "repository": str(ROOT), "compose": str(COMPOSE)}
    if marker.exists():
        if json.loads(marker.read_text()) != identity:
            raise ValueError("preparation ownership marker differs")
    elif cleanup:
        raise ValueError("refusing cleanup without an ownership marker")
    else:
        containers = run_capture(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"], 30
        ).stdout.strip()
        volumes = run_capture(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            30,
        ).stdout.strip()
        if containers or volumes:
            raise ValueError("refusing to claim a preexisting Compose project")
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(identity, stream)
    down = [
        *compose,
        "--profile",
        "campaign",
        "--profile",
        "preparation",
        "down",
        "--volumes",
        "--remove-orphans",
    ]
    run_capture([*compose, "config", "--quiet"], 30)
    readiness.unlink(missing_ok=True)
    run_capture(down, 30)
    if cleanup:
        marker.unlink()
        return
    deadline = started + 120

    def command(argv: list[str]) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ExternalCommandError("pair preparation exceeded its frozen deadline")
        return run_capture(argv, remaining).stdout

    try:
        command(
            [
                *compose,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "90",
                "--no-build",
                "core",
                "tracking",
            ]
        )
        command([*compose, "run", "--rm", "--no-deps", "prepare"])
        verified = command(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "prepare",
                "--confirm-core-database",
                "fulfillflow_core",
                "--confirm-tracking-database",
                "fulfillflow_tracking",
                "--verify-only",
            ]
        )
        if with_loadgen:
            command(
                [
                    *compose,
                    "--profile",
                    "campaign",
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "30",
                    "--no-build",
                    "loadgen",
                ]
            )
        if bundle is not None:
            from benchmarks.run_campaign import _verify_initial_state

            observed = DockerProbe(bundle, ROOT).observe()
            _verify_initial_state(bundle, SplitDatabaseProbe(observed.container_ids["postgres"]))
        elapsed = time.monotonic() - started
        if elapsed >= 120:
            raise ExternalCommandError("pair verification exceeded the frozen preparation deadline")
        report = json.loads(verified)
        report["preparation_seconds"] = elapsed
        readiness.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except BaseException as exc:
        evidence = os.environ.get("FULFILLFLOW_PREPARATION_EVIDENCE")
        if evidence:
            try:
                write_report(Path(evidence) / "error.json", error_report(exc))
                diagnostics(compose, Path(evidence) / "before-cleanup")
            except Exception:
                # Do not remove the only diagnostics if their export failed.
                raise exc from exc.__cause__
        try:
            run_capture(down, 30)
        except Exception as cleanup_error:
            if evidence:
                try:
                    write_report(Path(evidence) / "cleanup-error.json", error_report(cleanup_error))
                except OSError:
                    pass  # The original preparation error remains the primary failure.
        raise


def build_loadgen() -> str:
    """Preserve the exact parent; a local tag is necessary for Dockerfile FROM."""
    observed = run_capture(
        ["docker", "image", "inspect", FROZEN_LOADGEN, "--format", "{{.Id}}"], 30
    ).stdout.strip()
    # Docker's image store can return a manifest identity or a configuration identity.
    parent = json.loads(run_capture(["docker", "image", "inspect", FROZEN_LOADGEN], 30).stdout)[0]
    if not observed or FROZEN_LOADGEN not in {
        parent["Id"],
        *(value.rsplit("@", 1)[-1] for value in parent.get("RepoDigests", [])),
    }:
        raise ValueError("frozen loadgen identity is unavailable")
    run_capture(["docker", "tag", FROZEN_LOADGEN, FROZEN_TAG], 30)
    run_capture(
        [
            "docker",
            "build",
            "-f",
            str(ROOT / "Dockerfile.loadgen-v11"),
            "-t",
            "fulfillflow-loadgen:v11-candidate",
            str(ROOT),
        ],
        120,
    )
    return run_capture(
        ["docker", "image", "inspect", "fulfillflow-loadgen:v11-candidate", "--format", "{{.Id}}"],
        30,
    ).stdout.strip()


def audit_loadgen(candidate: str) -> dict[str, object]:
    script = (
        "import hashlib,json,importlib.metadata; from pathlib import Path; "
        "root=Path('/work/benchmarks'); "
        "print(json.dumps({'files':{str(p.relative_to(root)):"
        "hashlib.sha256(p.read_bytes()).hexdigest() "
        "for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}, "
        "'packages':sorted((d.metadata['Name'],d.version) "
        "for d in importlib.metadata.distributions())}))"
    )
    reports = []
    for image in (FROZEN_LOADGEN, candidate):
        reports.append(
            json.loads(
                run_capture(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "python",
                        image,
                        "-c",
                        script,
                    ],
                    30,
                ).stdout
            )
        )
    old, new = reports
    changed = sorted(
        key
        for key in old["files"].keys() | new["files"].keys()
        if old["files"].get(key) != new["files"].get(key)
    )
    if changed != ["campaign.py"] or old["packages"] != new["packages"]:
        raise ValueError("candidate loadgen changes more than the approved manifest module")
    if (
        new["files"]["campaign.py"]
        != hashlib.sha256((ROOT / "benchmarks/campaign.py").read_bytes()).hexdigest()
    ):
        raise ValueError("candidate loadgen does not contain the reviewed campaign module")
    return {
        "parent": FROZEN_LOADGEN,
        "candidate": candidate,
        "changed_files": changed,
        "locustfile_sha256": new["files"]["locustfile.py"],
        "packages_unchanged": True,
    }


def reuse_loadgen_audit(candidate: str, report_path: Path) -> dict[str, object]:
    """Reuse the completed immutable-image audit; check only the current campaign module."""
    sums = json.loads((report_path.parent / "SHA256SUMS.json").read_text(encoding="utf-8-sig"))
    if sums.get(report_path.name) != hashlib.sha256(report_path.read_bytes()).hexdigest():
        raise ValueError("stored loadgen audit checksum differs")
    report: dict[str, object] = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if (
        report.get("parent") != FROZEN_LOADGEN
        or report.get("candidate") != candidate
        or report.get("changed_files") != ["campaign.py"]
        or report.get("packages_unchanged") is not True
    ):
        raise ValueError("stored audit does not identify this approved derived image")
    observed = run_capture(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            candidate,
            "-c",
            "import hashlib; from pathlib import Path; "
            "print(hashlib.sha256(Path('/work/benchmarks/campaign.py').read_bytes()).hexdigest())",
        ],
        30,
    ).stdout.strip()
    if observed != hashlib.sha256((ROOT / "benchmarks/campaign.py").read_bytes()).hexdigest():
        raise ValueError("audited loadgen campaign module differs from the current source")
    return report


def materialize(project: str, destination: Path, audit_report: Path | None = None) -> None:
    """Record real identities and unchanged workload parameters in candidate manifests."""
    compose = compose_prefix(project)
    if run_capture(["git", "status", "--porcelain", "--untracked-files=no"], 10).stdout.strip():
        raise ValueError("commit the reviewed source before materializing candidates")
    sha = run_capture(["git", "rev-parse", "HEAD"], 10).stdout.strip()
    ids = {}
    images = {}
    for role, service in (
        ("core", "core"),
        ("tracking", "tracking"),
        ("postgres", "db"),
        ("loadgen", "loadgen"),
    ):
        identifier = run_capture([*compose, "ps", "-q", service], 30).stdout.strip()
        if not identifier:
            raise ValueError(f"missing prepared {role} container")
        ids[role] = identifier
        images[role] = run_capture(
            ["docker", "inspect", identifier, "--format", "{{.Image}}"], 30
        ).stdout.strip()
        if role in {"core", "tracking"}:
            image_info = json.loads(
                run_capture(["docker", "image", "inspect", images[role]], 30).stdout
            )[0]
            if (image_info.get("Config", {}).get("Labels") or {}).get(
                "org.opencontainers.image.revision"
            ) != sha:
                raise ValueError("candidate application image must identify the committed source")
    compatibility = (
        reuse_loadgen_audit(images["loadgen"], audit_report)
        if audit_report is not None
        else audit_loadgen(images["loadgen"])
    )
    probe = SplitDatabaseProbe(ids["postgres"])
    databases = {}
    targets: tuple[tuple[Owner, DatabaseProbe], ...] = (
        ("core", probe),
        ("tracking", probe.tracking),
    )
    for owner, db in targets:
        sections = {key: db._json_rows(sql) for key, sql in STRUCTURAL_SCHEMA_QUERIES.items()}
        schema = structural_schema_identity(
            sections, expected_table_names=OWNER_SCHEMA_TABLES[owner]
        )
        databases[owner] = {
            "structural_contract_version": schema.contract_version,
            "schema_sha256": schema.sha256,
            "alembic_heads": db.alembic_heads(),
        }
    destination.mkdir(parents=True, exist_ok=False)
    incomplete = destination / ".incomplete.json"
    incomplete.write_text('{"ready": false}\n', encoding="utf-8")
    for profile in ("mixed", "timeline", "ingestion"):
        baseline = json.loads(
            (ROOT / f"benchmarks/campaigns/v1-baseline-{profile}.json").read_text()
        )
        images["postgres"] = baseline["images"]["postgres"]
        baseline.update(
            schema_version=2,
            name=f"v11-candidate-{profile}",
            release="v1.1.0",
            git_sha=sha,
            images=images,
            database=databases,
            internal_timeouts={"core_seconds": 6, "tracking_seconds": 8},
        )
        baseline["environment"] = {
            "compose_project": project,
            "compose_file": COMPOSE.name,
            "services": {
                "app": None,
                "core": "core",
                "tracking": "tracking",
                "postgres": "db",
                "loadgen": "loadgen",
            },
        }
        baseline["resources"] = {
            "core": {"cpus": "1.0", "memory": "768m"},
            "tracking": {"cpus": "1.0", "memory": "768m"},
            "postgres": baseline["resources"]["postgres"],
            "loadgen": baseline["resources"]["loadgen"],
        }
        pool = dict(baseline["pool"], size=5)
        baseline["pool"] = {"core": pool, "tracking": pool}
        baseline["cohorts"]["dataset_manifest"] = os.path.relpath(
            ROOT / "benchmarks/datasets/benchmark-v1.0.json", destination
        ).replace("\\", "/")
        manifest = CampaignManifest.model_validate(baseline)
        target = destination / f"{manifest.name}.json"
        target.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        from benchmarks.run_campaign import _install_runtime_manifest, _verify_initial_state

        bundle = load_campaign(target)
        DockerProbe(bundle, ROOT).observe()
        _verify_initial_state(bundle, probe)
        installed = _install_runtime_manifest(bundle, target, ids["loadgen"], destination / profile)
        run_capture(
            [
                "docker",
                "exec",
                ids["loadgen"],
                "python",
                "-c",
                "import sys; from pathlib import Path; "
                "from benchmarks.campaign import load_campaign; "
                "b=load_campaign(Path(sys.argv[1])); "
                "assert b.manifest.release == 'v1.1.0' and b.manifest.git_sha == sys.argv[2]",
                installed,
                sha,
            ],
            30,
        )
    (destination / "loadgen-compatibility.json").write_text(
        json.dumps(compatibility, indent=2) + "\n", encoding="utf-8"
    )
    incomplete.unlink()
    checksums = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in destination.glob("*.json")
    }
    (destination / "SHA256SUMS.json").write_text(json.dumps(checksums, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["restore", "cleanup", "manifest", "build-loadgen"])
    parser.add_argument("--project", default="fulfillflow-benchmark-v11")
    parser.add_argument("--confirm-project")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--with-loadgen", action="store_true")
    parser.add_argument("--audit-report", type=Path)
    args = parser.parse_args()
    if args.action == "build-loadgen":
        print(build_loadgen())
    elif args.action == "manifest":
        if args.destination is None:
            parser.error("--destination is required")
        materialize(args.project, args.destination, args.audit_report)
    else:
        restore(
            args.project,
            args.confirm_project or "",
            cleanup=args.action == "cleanup",
            manifest=args.manifest,
            with_loadgen=args.with_loadgen,
        )
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        report = error_report(exc)
        evidence = os.environ.get("FULFILLFLOW_PREPARATION_EVIDENCE")
        if evidence and not (Path(evidence) / "error.json").exists():
            write_report(Path(evidence) / "error.json", report)
        print(json.dumps(report), file=sys.stderr)
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 2
    raise SystemExit(exit_code)

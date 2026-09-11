"""One manual attempt, then sealed evidence review, in a fixed eight-condition sequence."""

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from benchmarks import paired_controls
from benchmarks.campaign import CampaignManifest, load_campaign
from benchmarks.controls_v10 import (
    ControlError,
    _git,
    _run,
    _run_runner,
    _sha256,
    _source_environment,
    _write_checksums,
)
from benchmarks.host_probe import HostProbe
from benchmarks.operational_errors import diagnostics, error_report, write_report
from benchmarks.run_campaign import runner_provenance
from benchmarks.sensitivity_images import derive
from benchmarks.warmup_sensitivity import ORDER, PROTOCOL, load_sensitivity_campaign

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/results"
PACKAGE = RESULTS / "warmup-sensitivity-win9445-review-01"
REVIEW = RESULTS / "warmup-sensitivity-win9445-reviews-01"
PWSH = Path(
    r"C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
)


@dataclass(frozen=True)
class Attempt:
    number: int

    def __post_init__(self) -> None:
        if not 1 <= self.number <= 8:
            raise ControlError("attempt must be one of the eight fixed conditions")

    @property
    def version(self) -> Literal["v10", "v11"]:
        return ORDER[self.number - 1][0]

    @property
    def seconds(self) -> int:
        return ORDER[self.number - 1][1]

    @property
    def name(self) -> str:
        return f"warmup-sensitivity-win9445-01-{self.number:02d}-{self.version}-{self.seconds}s"

    @property
    def project(self) -> str:
        return f"fulfillflow-sensitivity-win9445-01-{self.number:02d}"

    @property
    def source(self) -> Path:
        return RESULTS / f"comparison-win9445-{self.version}-source-01"

    @property
    def candidate(self) -> Path:
        # This historical-shaped document is used only by frozen preparation/probes.
        return PACKAGE / "candidates" / f"{self.name}-preparation.json"

    @property
    def executable(self) -> Path:
        return PACKAGE / "candidates" / f"{self.name}.json"

    @property
    def attempt(self) -> Path:
        return RESULTS / self.name


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ControlError("expected a JSON object")
    return value


def environment(attempt: Attempt) -> dict[str, str]:
    result = paired_controls.environment_for(attempt, read(attempt.candidate))
    result["BENCH_APP_PORT"] = str(18040 + attempt.number)
    return result


def compose(attempt: Attempt) -> list[str]:
    document = read(attempt.candidate)
    return [
        "docker",
        "compose",
        "--project-name",
        attempt.project,
        "--file",
        str(attempt.source / document["environment"]["compose_file"]),
        "--file",
        str(PACKAGE / f"loadgen-{attempt.version}.json"),
    ]


def require_absent(attempt: Attempt) -> None:
    for argv in (
        ["docker", "ps", "--all", "--quiet"],
        ["docker", "volume", "ls", "--quiet"],
        ["docker", "network", "ls", "--quiet"],
    ):
        if _run(
            [*argv, "--filter", f"label=com.docker.compose.project={attempt.project}"], cwd=ROOT
        ).stdout.strip():
            raise ControlError("planned project already exists; no overwrite or retry")


def inputs() -> dict[str, str]:
    paths = [
        *ROOT.glob("benchmarks/*.py"),
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        ROOT / "scripts/Invoke-WarmupSensitivity.ps1",
        ROOT / "scripts/prepare_warmup_sensitivity.py",
    ]
    return {str(path.relative_to(ROOT)).replace("\\", "/"): _sha256(path) for path in paths}


def verify_package() -> dict[str, Any]:
    paired_controls.verify_checksums(PACKAGE)
    ready = read(PACKAGE / "ready.json")
    if ready["inputs"] != inputs() or ready["runner"] != runner_provenance(ROOT):
        raise ControlError("runner/coordinator/package identity changed; execution blocked")
    if ready["protocol"] != PROTOCOL or ready["order"] != [list(item) for item in ORDER]:
        raise ControlError("prepared protocol/order differs")
    return ready


def verify_candidate(attempt: Attempt) -> None:
    paired_controls.verify_source(attempt)
    bundle = load_sensitivity_campaign(attempt.executable)
    preparation = load_campaign(attempt.candidate)
    expected = preparation.manifest.model_dump(mode="json")
    expected.update(protocol=PROTOCOL, warmup_seconds=attempt.seconds)
    expected["timeouts"]["warmup_process_seconds"] += attempt.seconds - 60
    if bundle.manifest.model_dump(mode="json") != expected:
        raise ControlError("application preparation and executable protocol differ unexpectedly")
    originals = paired_controls.original_documents()
    original = originals[attempt.version]
    # Every frozen parameter is derived afresh; only declared identities/policy can vary.
    generated = candidate(
        attempt, originals, read(PACKAGE / "images.json")[attempt.version]["image"]
    )
    if read(attempt.candidate) != generated or preparation.manifest.git_sha != original["git_sha"]:
        raise ControlError("candidate differs from the immutable application reference")


def preflight(attempt: Attempt, destination: Path) -> None:
    verify_candidate(attempt)
    document = read(attempt.candidate)
    observed = {}
    for role, identifier in document["images"].items():
        info = json.loads(_run(["docker", "image", "inspect", identifier], cwd=ROOT).stdout)[0]
        if identifier not in {
            info["Id"],
            *(item.rsplit("@", 1)[-1] for item in info.get("RepoDigests", [])),
        }:
            raise ControlError("frozen image identity differs")
        observed[role] = info["Id"]
    bundle = load_sensitivity_campaign(attempt.executable)
    probe = HostProbe(
        bundle.manifest.host,
        official=False,
        timeout_seconds=bundle.manifest.timeouts.command_seconds,
    )
    host = probe.identity()
    # Existing dynamic admission also runs after stabilization inside the runner.
    probe.dynamic({})
    _run(
        [*compose(attempt), "--profile", "campaign", "config", "--quiet"],
        cwd=attempt.source,
        environment=environment(attempt),
    )
    paired_controls.source_python(
        attempt,
        """
import importlib.metadata, json, sys
from pathlib import Path
import benchmarks, fulfillflow
from benchmarks.campaign import load_campaign
bundle = load_campaign(Path(sys.argv[1]))
assert Path(sys.prefix).resolve() == (Path.cwd() / '.venv').resolve()
assert Path(benchmarks.__file__).resolve().is_relative_to(Path.cwd())
assert Path(fulfillflow.__file__).resolve().is_relative_to(Path.cwd())
assert importlib.metadata.version('fulfillflow') == bundle.manifest.release.removeprefix('v')
print(json.dumps({'source_valid': True, 'dataset_sha256': bundle.dataset_sha256}))
""",
        str(attempt.candidate),
        evidence=destination / "source.json",
    )
    write_report(destination / "identity.json", {"host": host, "images": observed})


def candidate(
    attempt: Attempt, originals: dict[str, dict[str, Any]], loadgen: str
) -> dict[str, Any]:
    document = copy.deepcopy(originals[attempt.version])
    reference = originals["v10"]
    document.update(name=attempt.name, official=False, repetitions=1, profile="mixed")
    document["weights"] = copy.deepcopy(reference["weights"])
    document["loads"] = [item for item in reference["loads"] if item["users"] == 12]
    document["environment"]["compose_project"] = attempt.project
    document["host"]["identity"]["os_build"] = "26200.9445"
    document["images"]["loadgen"] = loadgen
    document["cohorts"]["dataset_manifest"] = os.path.relpath(
        attempt.source / "benchmarks/datasets/benchmark-v1.0.json",
        attempt.candidate.parent,
    ).replace("\\", "/")
    CampaignManifest.model_validate(document)
    return document


def prepare_package(launcher: dict[str, Any]) -> None:
    runner = runner_provenance(ROOT)
    _git(ROOT, "ls-files", "--error-unmatch", *inputs())
    if _git(ROOT, "branch", "--show-current") != "codex/v1.1-tracking":
        raise ControlError("preparation requires the authorized branch")
    if Path(launcher.get("executable", "")).resolve() != PWSH.resolve() or not PWSH.is_file():
        raise ControlError("use the verified full PowerShell executable")
    if PACKAGE.exists() or REVIEW.exists() or any(Attempt(i).attempt.exists() for i in range(1, 9)):
        raise ControlError("preparation destinations already exist; preserve them")
    for number in range(1, 9):
        paired_controls.verify_source(Attempt(number))
        require_absent(Attempt(number))
    originals = paired_controls.original_documents()
    reference = CampaignManifest.model_validate(originals["v11"])
    probe = HostProbe(reference.host, official=False, timeout_seconds=30)
    probe.identity()
    probe.dynamic({})
    PACKAGE.mkdir()
    images = {
        version: derive(
            ROOT,
            PACKAGE / "images" / version,
            version,
            originals[version]["images"]["loadgen"],
            _git(ROOT, "rev-parse", "HEAD"),
        )
        for version in ("v10", "v11")
    }
    write_report(PACKAGE / "images.json", images)
    for version in images:
        write_report(
            PACKAGE / f"loadgen-{version}.json",
            {"services": {"loadgen": {"image": images[version]["image"]}}},
        )
    for number in range(1, 9):
        attempt = Attempt(number)
        document = candidate(attempt, originals, images[attempt.version]["image"])
        write_report(attempt.candidate, document)
        executable = copy.deepcopy(document)
        executable.update(protocol=PROTOCOL, warmup_seconds=attempt.seconds)
        executable["timeouts"]["warmup_process_seconds"] += attempt.seconds - 60
        write_report(attempt.executable, executable)
        preflight(attempt, PACKAGE / "preflight" / str(number))
    write_report(
        PACKAGE / "ready.json",
        {
            "protocol": PROTOCOL,
            "order": ORDER,
            "runner": runner,
            "inputs": inputs(),
            "launcher": launcher,
            "load_executed": False,
            "backup_confirmed": False,
            "matrix_eligible": False,
            "measurement_executed": False,
            "preparation": (
                "identities, datasets and Compose verified; "
                "clean DB preparation before each manual admission"
            ),
        },
    )
    _write_checksums(PACKAGE)


def require_next(attempt: Attempt) -> None:
    if attempt.attempt.exists():
        raise ControlError("attempt destination already exists; no retry or overwrite")
    for number in range(1, attempt.number):
        previous = Attempt(number)
        review_directory = REVIEW / f"{number:02d}"
        paired_controls.verify_checksums(review_directory)
        review = read(review_directory / "review.json")
        paired_controls.verify_checksums(previous.attempt)
        if review.get("release_next") is not True or review.get("checksums_sha256") != _sha256(
            previous.attempt / "checksums.sha256"
        ):
            raise ControlError("previous attempt has no intact approving evidence review")
    if any(Attempt(number).attempt.exists() for number in range(attempt.number + 1, 9)):
        raise ControlError("execution order differs from the fixed sequence")


def prepare_database(attempt: Attempt) -> int:
    verify_package()
    require_absent(attempt)
    verify_candidate(attempt)
    destination = attempt.attempt / "preparation"
    destination.mkdir(exist_ok=False)
    document = read(attempt.candidate)
    prefix = compose(attempt)
    env = environment(attempt)
    write_report(
        destination / "owned.json", {"project": attempt.project, "source": str(attempt.source)}
    )
    deadline = time.monotonic() + document["timeouts"]["preparation_seconds"]

    def command(argv: list[str], label: str) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError("frozen preparation deadline exceeded")
        _run(
            [*prefix, *argv],
            cwd=attempt.source,
            environment=env,
            timeout=remaining,
            evidence=destination / f"{label}.txt",
        )

    if attempt.version == "v10":
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
                f"{attempt.source}:/workspace:ro",
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
    verification = paired_controls.source_python(
        attempt,
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
print(json.dumps({'verified': True, 'container_ids': observed.container_ids,
                  'load_executed': False}))
""",
        str(attempt.candidate),
        evidence=destination / "verification.json",
        timeout=remaining,
    )
    if time.monotonic() >= deadline:
        raise ControlError("verification exceeded frozen preparation deadline")
    write_report(destination / "ready.json", verification)
    return 0


def execute(attempt: Attempt, launcher: dict[str, Any]) -> int:
    ready = verify_package()
    if launcher != ready["launcher"]:
        raise ControlError("PowerShell identity differs from preparation")
    require_next(attempt)
    require_absent(attempt)
    attempt.attempt.mkdir()
    report = {
        "complete": False,
        "stage": "preflight",
        "protocol": PROTOCOL,
        "coordinator": ready["runner"],
        "package_sha256": _sha256(PACKAGE / "checksums.sha256"),
        "matrix_eligible": False,
        "measurement_executed": False,
    }
    code = 2
    try:
        preflight(attempt, attempt.attempt / "preflight")
        prepare = [
            str(ROOT / ".venv/Scripts/python.exe"),
            "-X",
            "utf8",
            "-B",
            str(ROOT / "scripts/prepare_warmup_sensitivity.py"),
            "--prepare-step",
            str(attempt.number),
        ]
        report["stage"] = "runner"
        print(
            f"Starting {attempt.name}; at least {300 + attempt.seconds} s "
            "stabilization/admission; no measurement.",
            flush=True,
        )
        result = _run_runner(
            [
                str(ROOT / ".venv/Scripts/python.exe"),
                "-X",
                "utf8",
                "-B",
                "-m",
                "benchmarks.run_campaign",
                "--application-source",
                str(attempt.source),
                "--manifest",
                str(attempt.executable),
                "--warmup-sensitivity",
                "--execute",
                "--confirm-campaign",
                attempt.name,
                "--base-url",
                "http://app:8000" if attempt.version == "v10" else "http://core:8000",
                "--results-directory",
                str(attempt.attempt / "run"),
                "--prepare-command-json",
                json.dumps(prepare),
            ],
            ROOT,
            _source_environment(ROOT, environment(attempt)),
            attempt.attempt / "runner.txt",
        )
        code = result.returncode
        report["runner_exit_code"] = code
        metadata = read(attempt.attempt / "run/mixed-12-users-r01.partial/metadata.json")
        report["complete"] = metadata.get("observation_integrity_verified") is True
        if not report["complete"]:
            code = code or 2
    except BaseException as exc:
        report.update(error_report(exc))
        code = 130 if isinstance(exc, KeyboardInterrupt) else 2
    finally:
        try:
            if (attempt.attempt / "preparation/owned.json").is_file():
                # Export before stopping. Never remove containers, databases or volumes.
                capture_and_stop(attempt)
        except BaseException as exc:
            report["diagnostic_or_shutdown_error"] = error_report(exc)
            report["complete"] = False
            code = code or 2
        report["exit_code"] = code
        report["review_required"] = True
        write_report(attempt.attempt / "result.json", report)
        _write_checksums(attempt.attempt)
    print(f"Stopped without retry; review required: {attempt.attempt / 'result.json'}", flush=True)
    return code


def capture_and_stop(attempt: Attempt) -> None:
    owner = read(attempt.attempt / "preparation/owned.json")
    if owner != {"project": attempt.project, "source": str(attempt.source)}:
        raise ControlError("resource ownership differs; preserve for review")
    identifiers = _run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={attempt.project}",
        ],
        cwd=ROOT,
    ).stdout.split()
    observations = []
    for identifier in identifiers:
        info = json.loads(_run(["docker", "inspect", identifier], cwd=ROOT).stdout)[0]
        labels = info["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") != attempt.project:
            raise ControlError("container ownership differs")
        observations.append(
            {
                "id": info["Id"],
                "image": info["Image"],
                "service": labels.get("com.docker.compose.service"),
                "state": {
                    key: info["State"].get(key)
                    for key in (
                        "Status",
                        "Running",
                        "OOMKilled",
                        "ExitCode",
                        "StartedAt",
                        "FinishedAt",
                    )
                },
                "restart_count": info["RestartCount"],
            }
        )
    write_report(attempt.attempt / "container-final.json", observations)
    # Compose receives only synthetic frozen environment; raw config is never exported.
    saved = dict(os.environ)
    try:
        os.environ.update(environment(attempt))
        diagnostics(compose(attempt), attempt.attempt / "diagnostics", include_runtime=True)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    if identifiers:
        _run(
            ["docker", "stop", *identifiers],
            cwd=ROOT,
            timeout=120,
            evidence=attempt.attempt / "stop.txt",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan-only", action="store_true")
    group.add_argument("--prepare-only", action="store_true")
    group.add_argument("--execute", type=int)
    group.add_argument("--prepare-step", type=int)
    group.add_argument("--review", type=int)
    args = parser.parse_args()
    try:
        if args.plan_only:
            print(
                json.dumps(
                    {
                        "protocol": PROTOCOL,
                        "order": ORDER,
                        "destinations": [str(Attempt(i).attempt) for i in range(1, 9)],
                    },
                    indent=2,
                )
            )
            return 0
        if args.prepare_step:
            return prepare_database(Attempt(args.prepare_step))
        if args.review:
            from benchmarks.sensitivity_review import review_attempt

            review_attempt(Attempt(args.review))
            return 0
        launcher = json.load(sys.stdin)
        if args.prepare_only:
            prepare_package(launcher)
            return 0
        return execute(Attempt(args.execute), launcher)
    except BaseException as exc:
        print(json.dumps(error_report(exc)), file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())

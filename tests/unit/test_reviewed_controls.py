"""Reviewed campaigns preserve frozen cells and cannot repeat existing evidence."""

import copy
import json
import os
import shutil
import subprocess
import sys
import venv
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks import paired_controls as controls
from benchmarks.campaign import CampaignManifest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name in ("SERIES", "PACKAGE", "JOURNAL", "PROJECTS", "STEPS"):
        monkeypatch.setattr(controls, name, getattr(controls, name))
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    return tmp_path


def originals():
    result = {"v10": controls._baseline_document(controls.ROOT)}
    # The candidate fixture has the same topology; workload comes from immutable baseline blobs.
    from tests.unit.test_benchmark_v11 import candidate_payload

    result["v11"] = candidate_payload()
    result["v11"]["git_sha"] = controls.REVISIONS["v11"]
    for profile in ("timeline", "ingestion"):
        result[f"v10-{profile}"] = controls.read_json(
            controls.ROOT / f"benchmarks/campaigns/v1-baseline-{profile}.json"
        )
    return result


@pytest.mark.parametrize("series", ["abba", "official"])
def test_fixed_series_validates_with_original_manifest_rules(isolated, series):
    controls.configure_series(series)
    source = originals()
    before = copy.deepcopy(source)
    counts = Counter()
    for step in controls.STEPS:
        document = controls.candidate_document(step, source)
        manifest = CampaignManifest.model_validate(document)
        assert manifest.git_sha == controls.REVISIONS[step.version]
        assert manifest.warmup_quota_per_shipment == 430
        assert manifest.host.identity.os_build == "26200.9445"
        assert manifest.official is (series == "official")
        assert manifest.repetitions == (5 if series == "official" else 1)
        assert manifest.loads[0].users == step.users
        counts[step.version] += manifest.repetitions
        assert step.source.name == f"comparison-win9445-{step.version}-source-01"
    assert source == before
    expected_counts = {"v10": 30, "v11": 30} if series == "official" else {"v10": 2, "v11": 2}
    assert counts == expected_counts
    expected = ["v10", "v11", "v11", "v10"] * (3 if series == "official" else 1)
    assert [step.version for step in controls.STEPS] == expected


def test_official_execution_requires_completed_new_abba(isolated, monkeypatch):
    controls.configure_series("official")
    monkeypatch.setattr(controls, "prepared", lambda: pytest.fail("execution must remain blocked"))
    with pytest.raises(controls.ControlError, match="ABBA"):
        controls.execute_block({})
    assert not controls.JOURNAL.exists()


def test_prepare_command_keeps_series_and_source_explicit(isolated):
    controls.configure_series("abba")
    command = controls.preparation_argv(controls.STEPS[0], setup_only=True)
    assert command[command.index("--series") + 1] == "abba"
    assert "--setup-only" in command
    with pytest.raises(controls.ControlError, match="cannot be changed"):
        controls.configure_series("official")


def test_official_preparation_refuses_retry_after_partial_measurement(isolated):
    controls.configure_series("official")
    step = controls.STEPS[0]
    (step.attempt / "preparation/r01").mkdir(parents=True)
    controls.write_report(step.attempt / "run/mixed-4-users-r01/metadata.json", {"valid": False})
    with pytest.raises(controls.ControlError, match="previous repetition"):
        controls.prepare_step(step, setup_only=False)
    assert not (step.attempt / "preparation/r02").exists()


def test_official_second_repetition_exports_previous_before_new_restore(isolated, monkeypatch):
    controls.configure_series("official")
    step = controls.STEPS[0]
    controls.write_report(step.candidate, controls.candidate_document(step, originals()))
    previous = step.attempt / "preparation/r01"
    previous.mkdir(parents=True)
    controls.write_report(step.attempt / "run/mixed-4-users-r01/metadata.json", {"valid": True})
    calls = []
    monkeypatch.setattr(
        controls, "diagnostics", lambda *_a, **_k: calls.append("preserve-previous")
    )
    monkeypatch.setattr(controls, "cleanup", lambda *_: calls.append("clean-previous"))
    for name in ("verify_source", "verify_candidates", "assert_projects_absent"):
        monkeypatch.setattr(controls, name, lambda *_: None)
    monkeypatch.setattr(controls, "image_preflight", lambda *_: {})

    def run(argv, **_kwargs):
        calls.append("prepare-new")
        assert calls[:2] == ["preserve-previous", "clean-previous"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(controls, "_run", run)
    monkeypatch.setattr(controls, "source_python", lambda *_a, **_k: {"verified": True})
    assert controls.prepare_step(step, setup_only=False) == 0
    assert (step.attempt / "preparation/r02/ready.json").is_file()
    assert (step.attempt / "preparation/owned.json").is_file()


def test_host_runner_imports_are_isolated_from_measured_source(isolated):
    controls.configure_series("abba")
    step = controls.STEPS[0]
    (step.source / "src/fulfillflow").mkdir(parents=True)
    (step.source / "src/fulfillflow/__init__.py").write_text("raise RuntimeError('wrong source')")
    document = controls.candidate_document(step, originals())
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json,benchmarks.run_campaign,fulfillflow; "
            "print(json.dumps([benchmarks.run_campaign.__file__,fulfillflow.__file__]))",
        ],
        cwd=controls.ROOT,
        env=controls.runner_environment(step, document),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    runner, application_library = map(Path, json.loads(result.stdout))
    assert runner.resolve() == (controls.ROOT / "benchmarks/run_campaign.py").resolve()
    assert application_library.resolve().is_relative_to((controls.ROOT / "src").resolve())


@pytest.mark.parametrize("drift", [None, "missing-checksum", "runner", "application", "manifest"])
def test_abba_review_rejects_incomplete_hashes_and_identity_drift(isolated, monkeypatch, drift):
    controls.configure_series("official")
    host_runner = {"git": {"sha": "reviewed"}}
    monkeypatch.setattr(controls, "runner_provenance", lambda _: host_runner)
    package = isolated / "reviewed-abba-win9445-review-01"
    order = []
    for label, version in (("a1", "v10"), ("b1", "v11"), ("b2", "v11"), ("a2", "v10")):
        name = f"reviewed-abba-mixed-4-win9445-01-{label}-{version}"
        order.append(name)
        attempt = isolated / name
        candidate = package / f"candidates/{name}.json"
        controls.write_report(candidate, {"images": {"app": "frozen"}})
        controls.write_report(attempt / "candidate.json", controls.read_json(candidate))
        controls.write_report(attempt / "result.json", {"complete": True})
        repetition = attempt / "run/mixed-4-users-r01"
        metadata = {
            "valid": True,
            "campaign": name,
            "git": {"sha": controls.REVISIONS[version]},
            "host_runner": host_runner,
            "manifest_sha256": controls._sha256(candidate),
        }
        if label == "a1":
            if drift == "runner":
                metadata["host_runner"] = {}
            elif drift == "application":
                metadata["git"] = {"sha": "wrong"}
            elif drift == "manifest":
                metadata["manifest_sha256"] = "wrong"
        controls.write_report(repetition / "metadata.json", metadata)
        (repetition / "locust_stats.csv").write_text(
            "Name,Requests/s,95%\nAggregated," + ("190,35\n" if version == "v10" else "108,82\n")
        )
        controls._write_checksums(attempt)
        if label == "a1" and drift == "missing-checksum":
            (attempt / "checksums.sha256").write_text("")
    controls.write_report(package / "ready.json", {"order": order, "host_runner": host_runner})
    controls._write_checksums(package)
    journal = isolated / "reviewed-abba-win9445-execution-01"
    controls.write_report(
        journal / "result.json",
        {
            "complete": True,
            "exit_code": 0,
            "order": order,
            "completed": order,
            "package_ready_sha256": controls._sha256(package / "ready.json"),
        },
    )
    controls._write_checksums(journal)
    if drift:
        with pytest.raises(controls.ControlError):
            controls.abba_review()
    else:
        review = controls.abba_review()
        assert review["concordant"] is True
        assert len(review["rows"]) == 4
        assert len(review["contrasts"]) == 2
        assert review["statistical_equivalence_claim"] is False


def test_reviewed_attempt_uses_host_runner_and_explicit_measured_source(isolated, monkeypatch):
    controls.configure_series("abba")
    step = controls.STEPS[0]
    source = originals()
    controls.write_report(step.candidate, controls.candidate_document(step, source))
    for name in ("verify_source", "verify_candidates", "assert_projects_absent"):
        monkeypatch.setattr(controls, name, lambda *_: None)
    monkeypatch.setattr(controls, "image_preflight", lambda *_: {})
    monkeypatch.setattr(controls, "frozen_preflight", lambda *_: {})
    monkeypatch.setattr(controls, "environment_for", lambda *_: {})
    calls = []

    def run(argv, cwd, _env, _evidence):
        calls.append(argv)
        assert cwd == controls.ROOT
        assert argv[0] == str(controls.ROOT / ".venv/Scripts/python.exe")
        assert argv[argv.index("--application-source") + 1] == str(step.source)
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(controls, "_run_runner", run)
    assert controls.run_step(step, {}) == 7
    assert len(calls) == 1
    assert controls.read_json(step.attempt / "result.json")["complete"] is False


def test_idle_verification_is_fixed_and_never_starts_a_workload(isolated, monkeypatch):
    from benchmarks import run_campaign
    from tests.unit.test_benchmark_runner import _write_resources

    controls.configure_series("abba")
    step = controls.STEPS[0]
    observed = SimpleNamespace(
        container_ids={"app": "app-id", "postgres": "postgres-id", "loadgen": "loadgen-id"},
        postgres_user="user",
        postgres_database="database",
    )
    monkeypatch.setattr(controls, "load_campaign", lambda _: object())
    monkeypatch.setattr(
        controls, "DockerProbe", lambda *_: SimpleNamespace(observe=lambda: observed)
    )
    monkeypatch.setattr(controls, "DatabaseProbe", lambda *_: object())
    monkeypatch.setattr(run_campaign, "_run_phase", lambda *_: pytest.fail("load forbidden"))
    waits = []

    class Sampler:
        def __init__(self, path, *_args, **kwargs):
            assert kwargs["phase"] == "idle"
            self.output_path = path
            self.failed = SimpleNamespace(wait=lambda seconds: waits.append(seconds))

        def start(self):
            _write_resources(self.output_path)

        def stop(self):
            pass

    monkeypatch.setattr(controls, "ResourceSampler", Sampler)
    destination = isolated / "idle"
    controls.idle_verification(step, destination)
    report = controls.read_json(destination / "summary.json")
    assert waits == [300]
    assert report["load_executed"] is False
    assert report["business_requests_sent"] == 0
    assert report["under_load_stability_established"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher regression")
@pytest.mark.parametrize(
    "flag,mode",
    [("-PlanOnly", "--plan-only"), ("-PrepareOnly", "--prepare-only"), (None, "--execute")],
)
def test_real_reviewed_powershell_preserves_mode_and_visible_failure(tmp_path, flag, mode):
    executable = Path(os.environ.get("CONTROL_TEST_PWSH", "missing"))
    if not executable.is_file():
        pytest.skip("set CONTROL_TEST_PWSH to the verified full executable path")
    root = tmp_path / "repository with spaces"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts/Invoke-ReviewedControls.ps1"
    shutil.copyfile(controls.ROOT / "scripts/Invoke-ReviewedControls.ps1", script)
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (root / "benchmarks").mkdir()
    (root / "benchmarks/__init__.py").write_text("")
    (root / "benchmarks/paired_controls.py").write_text(
        "import json,sys\nidentity=json.load(sys.stdin)\n"
        f"assert sys.argv[1:] == ['--series','abba',{mode!r}]\n"
        "print('stage: synthetic failure; diagnostics: example.json', flush=True)\nsys.exit(7)\n"
    )
    result = subprocess.run(
        [str(executable), "-NoProfile", "-File", str(script), *([flag] if flag else [])],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=""),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 7
    assert "synthetic failure" in result.stdout
    assert "no automatic retry" in result.stderr

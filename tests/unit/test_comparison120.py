"""Symmetric protocol and fail-closed coordination; all execution children simulated."""

import copy
import json
import os
import shutil
import subprocess
import sys
import venv
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks import comparison_controls as comparison
from benchmarks import paired_controls as controls
from benchmarks import run_campaign as runner
from benchmarks.campaign import CampaignManifest, load_campaign
from benchmarks.comparison_protocol import ComparisonManifest, verify_warmup_progress
from benchmarks.sensitivity_controls import PWSH
from benchmarks.warmup_sensitivity import SensitivityManifest
from pydantic import ValidationError
from tests.unit.test_reviewed_controls import originals

os.environ["LOCUST_SKIP_MONKEY_PATCH"] = "1"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for name in ("SERIES", "PACKAGE", "JOURNAL", "PROJECTS", "STEPS"):
        monkeypatch.setattr(controls, name, getattr(controls, name))
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    controls.configure_series("comparison120")
    controls.PACKAGE.mkdir()
    controls.write_report(
        controls.PACKAGE / "images.json",
        {v: {"image": "sha256:" + "1" * 64} for v in ("v10", "v11")},
    )
    source = originals()
    for step in controls.STEPS:
        controls.write_report(step.candidate, controls.candidate_document(step, source))
        controls.write_report(comparison.executable(step), comparison.candidate_execution(step))
    return controls.STEPS


def test_fixed_symmetric_matrix_and_exact_policy_delta(configured):
    assert [s.version for s in configured] == ["v10", "v11", "v11", "v10"] * 3
    assert len({s.attempt for s in configured}) == 12
    for step in configured:
        original = controls.read_json(step.candidate)
        execution = controls.read_json(comparison.executable(step))
        assert CampaignManifest.model_validate(original).warmup_seconds == 60
        actual = ComparisonManifest.model_validate(execution)
        assert actual.loads[0].users == step.users
        assert actual.repetitions == 5 and actual.official
        assert actual.timeouts.warmup_process_seconds == 150
        changed = copy.deepcopy(execution)
        changed.pop("protocol")
        changed["warmup_seconds"] = 60
        changed["timeouts"]["warmup_process_seconds"] -= 60
        assert changed == original
        for model in (CampaignManifest, SensitivityManifest):
            with pytest.raises(ValidationError):
                model.model_validate(execution)


@pytest.mark.parametrize(
    "key,value",
    [
        ("warmup_seconds", 60),
        ("repetitions", 1),
        ("official", False),
        ("warmup_quota_per_shipment", 428),
        ("stabilization_seconds", 0),
        ("collection_interval_seconds", 2),
        ("protocol", "unknown"),
    ],
)
def test_comparison_contract_rejects_drift(configured, key, value):
    doc = controls.read_json(comparison.executable(configured[0]))
    doc[key] = value
    with pytest.raises(ValidationError):
        ComparisonManifest.model_validate(doc)


def progress(users=4):
    return dict(
        schema_version=1,
        admission_seconds=120,
        registered_users=users,
        in_flight=0,
        failure_code=None,
        warmup_complete=True,
        applied_by_user=[430] * users,
    )


@pytest.mark.parametrize("defect", [None, "quota", "drain", "reason", "balanced-total"])
def test_per_user_quota_and_drain(tmp_path, defect):
    doc = progress()
    if defect == "quota":
        doc["applied_by_user"][0] -= 1
    if defect == "balanced-total":
        doc["applied_by_user"] = [429, 431, 430, 430]
    if defect == "drain":
        doc["in_flight"] = 1
    if defect == "reason":
        doc["failure_code"] = "quota_incomplete"
    (tmp_path / "warmup-progress.json").write_text(json.dumps(doc))
    if defect:
        with pytest.raises(ValueError):
            verify_warmup_progress(tmp_path, 4)
    else:
        verify_warmup_progress(tmp_path, 4)


@pytest.mark.parametrize("defect", [None, "quota", "database", "process"])
def test_measurement_requires_both_warmup_gates(configured, tmp_path, monkeypatch, defect):
    manifest = ComparisonManifest.model_validate(
        controls.read_json(comparison.executable(configured[0]))
    )
    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")), manifest=manifest
    )
    monkeypatch.setattr(
        runner, "_git_provenance", lambda _: runner.GitProvenance(manifest.git_sha, "", True, True)
    )
    monkeypatch.setattr(runner, "_project_release", lambda _: manifest.release)
    monkeypatch.setattr(
        runner,
        "HostProbe",
        lambda *_a, **_k: SimpleNamespace(identity=lambda: {}, dynamic=lambda _: {}),
    )
    observed = SimpleNamespace(
        container_ids={"postgres": "p", "loadgen": "l"}, postgres_user="u", postgres_database="d"
    )
    monkeypatch.setattr(runner, "DockerProbe", lambda *_: SimpleNamespace(observe=lambda: observed))
    monkeypatch.setattr(
        runner, "DatabaseProbe", lambda *_a, **_k: SimpleNamespace(snapshot=lambda _: None)
    )
    for name in ("_run_preparation", "_stabilize", "_install_runtime_manifest"):
        monkeypatch.setattr(runner, name, lambda *_: None)
    monkeypatch.setattr(runner, "_verify_initial_state", lambda *_: (None, None))
    order = []

    class ReachedMeasurement(Exception):
        pass

    def phase(*args):
        order.append(args[2])
        if args[2] == "measurement":
            raise ReachedMeasurement
        if defect == "process":
            raise runner.CampaignExecutionError("process exit 2")
        directory = args[-1] / "warmup"
        directory.mkdir()
        doc = progress()
        if defect == "quota":
            doc["applied_by_user"][0] -= 1
        (directory / "warmup-progress.json").write_text(json.dumps(doc))

    monkeypatch.setattr(runner, "_run_phase", phase)

    def reconcile(*_):
        if defect == "database":
            raise runner.CampaignExecutionError("reconciliation failed")

    monkeypatch.setattr(runner, "_verify_warmup", reconcile)
    expected = ReachedMeasurement if defect is None else (ValueError, runner.CampaignExecutionError)
    with pytest.raises(expected):
        runner._execute(
            bundle, configured[0].candidate, "http://simulated", tmp_path / "run", ["simulated"]
        )
    assert order == (["warmup", "measurement"] if defect is None else ["warmup"])
    assert (tmp_path / "run/.incomplete.json").exists()


@pytest.mark.parametrize("failure", [None, "warmup", "review", "identity"])
def test_sequence_stops_without_retry_and_preserves_journal(configured, monkeypatch, failure):
    calls = []
    monkeypatch.setattr(comparison, "verify_release", lambda: None)
    controls.write_report(controls.PACKAGE / "review.json", {"launcher": {}})
    controls._write_checksums(controls.PACKAGE)
    monkeypatch.setattr(controls, "assert_projects_absent", lambda: None)
    monkeypatch.setattr(controls, "verify_checksums", lambda _: None)

    def run(step, _):
        calls.append(step.number)
        step.attempt.mkdir()
        (step.attempt / "preserved.txt").write_text("evidence")
        return 2 if step.number == 3 and failure == "warmup" else 0

    def review(step):
        if step.number == 3 and failure == "review":
            raise ControlError("invalid evidence")

    from benchmarks.controls_v10 import ControlError

    monkeypatch.setattr(controls, "run_step", run)
    monkeypatch.setattr(comparison, "verify_block", review)
    if failure == "identity":
        monkeypatch.setattr(
            comparison, "verify_release", lambda: (_ for _ in ()).throw(ControlError("identity"))
        )
        with pytest.raises(ControlError):
            comparison.execute({})
        assert calls == []
        return
    assert comparison.execute({}) == (2 if failure else 0)
    assert calls == list(range(1, 4 if failure else 13))
    assert all((configured[n - 1].attempt / "preserved.txt").exists() for n in calls)
    report = controls.read_json(controls.JOURNAL / "result.json")
    assert report["complete"] is (failure is None)
    with pytest.raises(ControlError, match="already exist"):
        comparison.execute({})


def test_unreleased_review_cannot_start(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(comparison, "verify_draft", lambda: {})
    monkeypatch.setattr(comparison, "RELEASE", tmp_path / "not-released")
    with pytest.raises(controls.ControlError, match="awaits independent review"):
        comparison.verify_release()


@pytest.fixture(scope="module")
def fake_launcher_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("comparison") / "repo with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "benchmarks").mkdir()
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    shutil.copy(Path("scripts/Invoke-Comparison120.ps1"), root / "scripts")
    (root / "benchmarks/__init__.py").touch()
    (root / "benchmarks/comparison_controls.py").write_text(
        "import sys,os,json\nfrom pathlib import Path\n"
        "launcher=json.load(sys.stdin)\n"
        "Path(os.environ['SIM_LOG']).write_text(json.dumps({'args':sys.argv[1:],'launcher':launcher}))\n"
        "sys.exit(int(os.environ['SIM_EXIT']))\n"
    )
    return root


@pytest.mark.parametrize("code", [0, 2, 130])
@pytest.mark.skipif(sys.platform != "win32", reason="requires actual Windows PowerShell")
def test_real_powershell_blocking_spaces_other_cwd(fake_launcher_repo, tmp_path, code):
    log = tmp_path / "events.json"
    env = {**os.environ, "SIM_LOG": str(log), "SIM_EXIT": str(code)}
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(PWSH),
            "-NoProfile",
            "-File",
            str(fake_launcher_repo / "scripts/Invoke-Comparison120.ps1"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == code, result.stdout + result.stderr
    data = json.loads(log.read_text())
    assert data["args"] == ["--execute"]
    assert Path(data["launcher"]["executable"]).resolve() == PWSH.resolve()


@pytest.mark.parametrize("users", [4, 12])
def test_reused_schedule_scales_every_offset(users):
    from benchmarks.locustfile import _warmup_scheduled_offset

    for sequence in range(430):
        for slot in range(users):
            original = (sequence * users + slot + 1) * 60 / (430 * users + 1)
            assert _warmup_scheduled_offset(sequence, 430, users, slot) == original
            assert _warmup_scheduled_offset(sequence, 430, users, slot, 120) == original * 2


@pytest.mark.parametrize("defect", [None, "commit", "branch", "status", "conclusion", "source"])
def test_final_seal_requires_exact_ci_and_reviewed_sources(
    configured, monkeypatch, tmp_path, defect
):
    release = tmp_path / "release"
    monkeypatch.setattr(comparison, "RELEASE", release)
    monkeypatch.setattr(
        comparison, "verify_draft", lambda: {"runner": {"components": {"a": "hash"}}}
    )
    monkeypatch.setattr(
        comparison,
        "runner_provenance",
        lambda _: {"components": {"a": "changed" if defect == "source" else "hash"}},
    )

    def git(_root, *args):
        return "codex/v1.1-tracking" if args[0] == "branch" else "a" * 40

    monkeypatch.setattr(comparison, "_git", git)
    controls._write_checksums(controls.PACKAGE)
    ci = dict(
        headSha="a" * 40,
        headBranch="codex/v1.1-tracking",
        status="completed",
        conclusion="success",
        url="ci",
    )
    for field, cause in [
        ("headSha", "commit"),
        ("headBranch", "branch"),
        ("status", "status"),
        ("conclusion", "conclusion"),
    ]:
        if defect == cause:
            ci[field] = "different"
    monkeypatch.setattr(
        comparison, "_run", lambda *_a, **_k: SimpleNamespace(stdout=json.dumps(ci))
    )
    if defect:
        with pytest.raises(controls.ControlError):
            comparison.seal_execution("123", "explicit user review approval")
        assert not release.exists()
    else:
        comparison.seal_execution("123", "explicit user review approval")
        controls.verify_checksums(release)
        assert controls.read_json(release / "ready.json")["ci_commit"] == "a" * 40


def test_host_mismatch_blocks_before_image_derivation(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(comparison, "ROOT", tmp_path)
    monkeypatch.setattr(controls, "PACKAGE", tmp_path / "new-review")
    monkeypatch.setattr(comparison, "RELEASE", tmp_path / "new-release")
    executable = tmp_path / "pwsh.exe"
    executable.touch()
    monkeypatch.setattr(comparison, "PWSH", executable)
    monkeypatch.setattr(comparison, "_git", lambda *_: "codex/v1.1-tracking")
    monkeypatch.setattr(comparison, "_run", lambda *_a, **_k: None)
    monkeypatch.setattr(controls, "original_documents", originals)
    monkeypatch.setattr(comparison, "derive", lambda *_: pytest.fail("must not derive images"))
    from benchmarks.collectors import EnvironmentMismatchError

    def identity():
        raise EnvironmentMismatchError({"os_build": {"matches": False}})

    monkeypatch.setattr(
        comparison, "HostProbe", lambda *_a, **_k: SimpleNamespace(identity=identity)
    )
    with pytest.raises(EnvironmentMismatchError):
        comparison.prepare_review({"executable": str(executable), "version": "7"})
    assert not controls.PACKAGE.exists()


@pytest.mark.parametrize("defect", [None, "users", "name", "spawn", "protocol"])
@pytest.mark.parametrize("is_continuation", [False, True])
def test_real_metadata_allows_second_preparation_only_after_valid_first(
    configured, monkeypatch, defect, is_continuation
):
    from datetime import UTC, datetime

    step = replace(configured[0], continuation=is_continuation)
    manifest_path = comparison.executable(step)
    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")),
        manifest=ComparisonManifest.model_validate(controls.read_json(manifest_path)),
    )
    monkeypatch.setattr(comparison, "load_comparison_campaign", lambda _: bundle)
    git = runner.GitProvenance(bundle.manifest.git_sha, "", True, True)
    host_runner = {"identity": "reviewed-host"}
    monkeypatch.setattr(comparison, "runner_provenance", lambda _: host_runner)
    snapshot = SimpleNamespace(label="synthetic", metrics=lambda: {})
    phase = SimpleNamespace(
        phase="warmup", started_at=datetime.now(UTC), finished_at=datetime.now(UTC), returncode=0
    )
    metadata = runner._metadata(
        bundle,
        manifest_path,
        Path.cwd(),
        bundle.manifest.loads[0],
        step.first_repetition,
        git,
        SimpleNamespace(checks={}, container_ids={}),
        snapshot,
        snapshot,
        snapshot,
        git,
        git,
        phase,
        phase,
        {},
        {},
        {},
        host_runner=host_runner,
    )
    assert "loads" not in metadata["protocol_expected"]
    if defect == "protocol":
        metadata["protocol_expected"]["warmup_seconds"] = 60
    elif defect:
        key = {"users": "users", "name": "name", "spawn": "spawn_rate"}[defect]
        metadata["load"][key] = "different"
    first = step.attempt / f"run/mixed-4-users-r{step.first_repetition:02d}"
    controls.write_report(first / "metadata.json", metadata)
    controls.write_report(first / "warmup/warmup-progress.json", progress())
    controls._write_checksums(first)
    (step.attempt / f"preparation/r{step.first_repetition:02d}").mkdir(parents=True)
    events = []
    monkeypatch.setattr(controls, "diagnostics", lambda *_a, **_k: events.append("diagnostics"))
    monkeypatch.setattr(controls, "cleanup", lambda *_: events.append("cleanup"))

    def reached_next_preparation(_):
        events.append("prepare-r02")
        raise controls.ControlError("simulated preparation stop before Docker")

    monkeypatch.setattr(controls, "verify_source", reached_next_preparation)
    if defect:
        with pytest.raises(controls.ControlError, match="identity and completion"):
            controls.prepare_step(step, setup_only=False)
        assert events == []
        assert not (step.attempt / f"preparation/r{step.first_repetition + 1:02d}").exists()
    else:
        assert controls.prepare_step(step, setup_only=False) == 2
        assert events == ["diagnostics", "cleanup", "prepare-r02"]
        assert (step.attempt / f"preparation/r{step.first_repetition + 1:02d}/error.json").exists()
    controls.verify_checksums(first)


def test_preparation_child_error_is_preserved_and_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_TOKEN", "do-not-export")
    path = tmp_path / "preparation-error.json"
    child = (
        "import sys; "
        "sys.stderr.write('comparison repetition did not pass identity and completion gates; "
        "token=do-not-export'); sys.exit(2)"
    )
    with pytest.raises(runner.CampaignExecutionError, match="subprocess exited 2"):
        runner._run_preparation([sys.executable, "-c", child], 10, path)
    report = controls.read_json(path)
    assert report["process_returncode"] == 2
    assert report["stage"] == "preparation" and report["phase"] is None
    assert report["shutdown"] is None
    assert "identity and completion gates" in report["child_stderr"]
    assert "do-not-export" not in path.read_text()
    assert "argv" not in report


def test_preparation_primary_and_shutdown_failures_stay_separate(tmp_path, monkeypatch):
    class Process:
        def wait(self, _):
            return 2

        def ensure_stopped(self):
            raise runner.CampaignExecutionError("simulated teardown failure")

    monkeypatch.setattr(runner, "ManagedProcess", lambda *_a, **_k: Process())
    path = tmp_path / "preparation-error.json"
    with pytest.raises(runner.CampaignExecutionError, match="subprocess exited 2"):
        runner._run_preparation(["simulated"], 10, path)
    report = controls.read_json(path)
    assert "exited 2" in report["primary"]["errors"][0]["message"]
    assert "teardown" in report["shutdown"]["errors"][0]["message"]


def test_preparation_timeout_retains_deadline_and_stops_child(tmp_path, monkeypatch):
    calls = []

    class Process:
        def wait(self, timeout):
            calls.append(timeout)
            raise runner.ProcessTimeoutError("external process exceeded its frozen timeout")

        def ensure_stopped(self):
            calls.append("stopped")

    monkeypatch.setattr(runner, "ManagedProcess", lambda *_a, **_k: Process())
    path = tmp_path / "preparation-error.json"
    with pytest.raises(runner.ProcessTimeoutError):
        runner._run_preparation(["simulated"], 120, path)
    assert calls == [120, "stopped"]
    report = controls.read_json(path)
    assert report["process_returncode"] is None
    assert report["primary"]["errors"][0]["type"] == "ProcessTimeoutError"


def test_preparation_export_failure_retains_original_cause(tmp_path, monkeypatch):
    class Process:
        def wait(self, _):
            return 2

        def ensure_stopped(self):
            pass

    def fail_export(*_):
        raise OSError("simulated write failure")

    monkeypatch.setattr(runner, "ManagedProcess", lambda *_a, **_k: Process())
    monkeypatch.setattr(runner, "_write_json", fail_export)
    with pytest.raises(runner.DiagnosticExportError, match="diagnostic export failed") as caught:
        runner._run_preparation(["simulated"], 120, tmp_path / "error.json")
    assert "subprocess exited 2" in str(caught.value.__cause__)

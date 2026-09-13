"""Nonofficial contract and supervision, with no benchmark or native power changes."""

import json
import os
import shutil
import signal
import subprocess
import venv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ["LOCUST_SKIP_MONKEY_PATCH"] = "1"

from benchmarks import active_screen_controls as active
from benchmarks import comparison_controls as comparison
from benchmarks import locustfile, run_campaign
from benchmarks import paired_controls as controls
from benchmarks.active_screen_energy import EnergyConditionError, require_energy
from benchmarks.active_screen_protocol import PROTOCOL, ActiveScreenManifest
from benchmarks.campaign import CampaignManifest
from benchmarks.comparison_protocol import ComparisonManifest
from benchmarks.sensitivity_controls import PWSH
from locust.env import Environment
from pydantic import ValidationError
from tests.unit.test_comparison120 import configured  # noqa: F401


@pytest.fixture
def diagnostic(configured):  # noqa: F811
    step = next(s for s in configured if s.version == "v11" and s.users == 4)
    doc = json.loads(comparison.executable(step).read_text())
    doc.update(protocol=PROTOCOL, official=False, matrix_eligible=False)
    return doc


def test_separate_contract_keeps_frozen_policy(diagnostic):
    model = ActiveScreenManifest.model_validate(diagnostic)
    assert model.repetitions == 5 and model.warmup_seconds == 120
    for historical in (CampaignManifest, ComparisonManifest):
        with pytest.raises(ValidationError):
            historical.model_validate(diagnostic)


@pytest.mark.parametrize(
    "field,value",
    [
        ("official", True),
        ("matrix_eligible", True),
        ("repetitions", 4),
        ("profile", "ingestion"),
        ("warmup_seconds", 60),
        ("warmup_quota_per_shipment", 429),
        ("collection_interval_seconds", 2),
        ("stabilization_seconds", 299),
    ],
)
def test_diagnostic_rejects_drift(diagnostic, field, value):
    diagnostic[field] = value
    with pytest.raises(ValidationError):
        ActiveScreenManifest.model_validate(diagnostic)


def test_protocol_conflicts_fail_before_runtime(monkeypatch):
    monkeypatch.setenv("BENCHMARK_CAMPAIGN_MANIFEST", "not-read.json")
    monkeypatch.setenv("BENCHMARK_ACTIVE_SCREEN_PROTOCOL", PROTOCOL)
    monkeypatch.setenv("BENCHMARK_COMPARISON_PROTOCOL", "symmetric-warmup120-comparison-v1")
    with pytest.raises(RuntimeError, match="choose one"):
        locustfile._initialize(None)


@pytest.mark.parametrize("phase", ["warmup", "measurement"])
def test_active_contract_exports_per_user_final_warmup_only(tmp_path, monkeypatch, phase):
    monkeypatch.delenv("BENCHMARK_COMPARISON_PROTOCOL", raising=False)
    monkeypatch.delenv("BENCHMARK_WARMUP_SENSITIVITY", raising=False)
    monkeypatch.setenv("BENCHMARK_ACTIVE_SCREEN_PROTOCOL", PROTOCOL)
    monkeypatch.setenv("BENCHMARK_PHASE", phase)
    monkeypatch.setenv("BENCHMARK_RESPONSE_CODES_FILE", str(tmp_path / "codes.csv"))
    monkeypatch.setenv("BENCHMARK_OPERATIONAL_RESULTS_FILE", str(tmp_path / "operations.csv"))
    monkeypatch.setattr(locustfile, "_TALLY", locustfile.ResponseTally())
    runtime = SimpleNamespace(
        bundle=SimpleNamespace(manifest=SimpleNamespace(warmup_seconds=120)),
        _registered=4,
        in_flight=0,
        failure_code=None,
        warmup_complete=True,
        warmup_completed_for=lambda index: 430,
    )
    monkeypatch.setattr(locustfile, "_require_runtime", lambda: runtime)
    locustfile._write_response_artifacts(Environment())
    path = tmp_path / "warmup-progress.json"
    assert path.exists() is (phase == "warmup")
    if path.exists():
        assert json.loads(path.read_text())["applied_by_user"] == [430] * 4


@pytest.mark.parametrize("change", ["fresh", "stale", "off", "failed", "missing"])
def test_guard_requires_fresh_positive_evidence(tmp_path, monkeypatch, change):
    path = tmp_path / "guard.json"
    monkeypatch.setenv("FULFILLFLOW_ACTIVE_GUARD", str(path))
    state = {
        "ready": True,
        "failure": "",
        "display": 1,
        "heartbeat_utc": datetime.now(UTC).isoformat(),
    }
    if change == "stale":
        state["heartbeat_utc"] = (datetime.now(UTC) - timedelta(seconds=4)).isoformat()
    if change == "off":
        state["display"] = 0
    if change == "failed":
        state["failure"] = "session_locked"
    if change != "missing":
        path.write_text(json.dumps(state))
    if change == "fresh":
        require_energy()
    else:
        with pytest.raises(EnergyConditionError):
            require_energy()


def test_environment_failure_interrupts_wait_without_waiting_for_child():
    process = object.__new__(run_campaign.ManagedProcess)

    def failed():
        raise EnergyConditionError("screen_off")

    with pytest.raises(EnergyConditionError):
        process.wait(330, health_check=failed)


def test_result_export_failure_preserves_primary_stderr(tmp_path, monkeypatch, capsys):
    def failed(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(active, "write_report", failed)
    report = {"primary": {"stage": "process_exit", "exit_code": 2}}
    assert active.persist_result(tmp_path, report) is False
    recovered = json.loads(capsys.readouterr().err)
    assert recovered["primary"] == {"stage": "process_exit", "exit_code": 2}
    assert recovered["artifact_export"]["errors"][0]["type"] == "OSError"
    assert recovered["exit_code"] == 2 and recovered["complete"] is False


def test_runner_mode_is_explicit_without_reclassifying_legacy_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(
        run_campaign,
        "_git_provenance",
        lambda p: run_campaign.GitProvenance("sha", "branch", True, True),
    )
    monkeypatch.setattr(run_campaign, "_file_sha256", lambda p: "hash")
    legacy = run_campaign.runner_provenance(tmp_path)
    current = run_campaign.runner_provenance(
        tmp_path, diagnostic_mode=run_campaign.ACTIVE_SCREEN_RUNNER_MODE
    )
    assert legacy["diagnostic_mode"] == "explicit-warmup-only-not-matrix-eligible"
    assert (
        current.pop("diagnostic_mode") == "active-screen-warmup-and-measurement-not-matrix-eligible"
    )
    legacy.pop("diagnostic_mode")
    assert current == legacy


@pytest.mark.parametrize(
    "defect", [None, "ci_head_sha", "ci_conclusion", "isolation_review_approved"]
)
def test_release_requires_exact_ci_and_isolation_review(monkeypatch, defect):
    fingerprints = {
        "scripts/ActiveScreenGuard.cs": "hash",
        "scripts/Invoke-ActiveScreenDiagnostic.ps1": "hash",
    }
    monkeypatch.setattr(
        active, "verify_package", lambda: {"inputs": fingerprints, "power_settings": "power"}
    )
    ready = {
        "package_sha256": "hash",
        "approved": True,
        "inputs": fingerprints,
        "idle_result_sha256": "hash",
        "git_sha": "sha",
        "ci_head_sha": "sha",
        "ci_conclusion": "success",
        "isolation_review_approved": True,
    }
    if defect:
        ready[defect] = "mismatch"
    idle = {
        "exit_code": 0,
        "released": True,
        "mode": "IdleCheck",
        "duration_seconds": 960,
        "load_executed": False,
        "guard_sha256": "hash",
        "launcher_sha256": "hash",
    }
    monkeypatch.setattr(controls, "read_json", lambda p: ready if p.name == "ready.json" else idle)
    monkeypatch.setattr(controls, "verify_checksums", lambda p: None)
    monkeypatch.setattr(active, "_sha256", lambda p: "hash")
    monkeypatch.setattr(active, "_git", lambda *a: "sha")
    monkeypatch.setattr(active, "runner_provenance", lambda *a, **k: {})
    monkeypatch.setattr(active, "_run", lambda *a, **k: SimpleNamespace(stdout="power"))
    if defect:
        with pytest.raises(controls.ControlError):
            active.require_release()
    else:
        active.require_release()


@pytest.mark.parametrize("fail_review", [False, True])
def test_real_preparation_sequence_reviews_before_cleaning(tmp_path, monkeypatch, fail_review):
    for field in ("SERIES", "PACKAGE", "PROJECTS", "STEPS", "RESULTS"):
        monkeypatch.setattr(controls, field, getattr(controls, field))
    monkeypatch.setattr(active, "PACKAGE", tmp_path / "package")
    monkeypatch.setattr(controls, "RESULTS", tmp_path / "results")
    step = active.configure()
    controls.write_report(step.candidate, {"timeouts": {"preparation_seconds": 120}})
    for name in ("verify_source", "verify_candidates", "assert_projects_absent", "image_preflight"):
        monkeypatch.setattr(controls, name, lambda *a: None)
    monkeypatch.setattr(controls, "environment_for", lambda *a: {})
    monkeypatch.setattr(controls, "_run", lambda *a, **k: None)
    monkeypatch.setattr(controls, "source_python", lambda *a, **k: {"verified": True})
    calls = []

    def review(directory):
        calls.append("review")
        if fail_review:
            raise controls.ControlError("simulated integrity failure")

    monkeypatch.setattr(active, "verify_repetition", review)
    monkeypatch.setattr(active, "capture", lambda *a: calls.append("capture"))
    monkeypatch.setattr(controls, "diagnostics", lambda *a, **k: calls.append("export"))
    monkeypatch.setattr(controls, "cleanup", lambda *a: calls.append("cleanup"))
    for n in range(1, 6):
        if fail_review and n == 2:
            with pytest.raises(controls.ControlError, match="integrity"):
                controls.prepare_step(step, setup_only=False)
            assert calls == ["review"]
            assert not (step.attempt / "preparation/r02").exists()
            return
        assert controls.prepare_step(step, setup_only=False) == 0
        controls.write_report(
            step.attempt / f"run/mixed-4-users-r{n:02d}/metadata.json", {"valid": True}
        )
    assert calls == ["review", "capture", "export", "cleanup"] * 4
    with pytest.raises(controls.ControlError, match="already exist"):
        controls.prepare_step(step, setup_only=False)


@pytest.mark.skipif(os.name != "nt", reason="real Windows PowerShell host required")
@pytest.mark.parametrize("fail_at", [0, 3, "uncooperative", "deadline", "idle"])
def test_real_powershell_simulated_five_children_and_no_overwrite(tmp_path, fail_at):
    root = tmp_path / "path with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    shutil.copyfile(source / "scripts/Invoke-ActiveScreenDiagnostic.ps1", scripts / "Invoke.ps1")
    if fail_at in {"uncooperative", "deadline"}:
        launcher = scripts / "Invoke.ps1"
        text = launcher.read_text()
        assert text.count("-TimeoutMilliseconds 180000") == 1
        # Shorten only the copied test fixture; production retains its fixed 180 seconds.
        launcher.write_text(text.replace("-TimeoutMilliseconds 180000", "-TimeoutMilliseconds 100"))
    (scripts / "ActiveScreenGuard.cs").write_text("""
using System;
public class ActiveScreenGuard : IDisposable {
 public bool Ready=true, Released=false; public string Failure=""; public int Display=1;
 public long HeartbeatTicks { get { return DateTime.UtcNow.Ticks; } }
 public string[] Events(){return new string[]{"simulated"};}
 public void Dispose(){Released=true;}
}""")
    if fail_at == "deadline":
        launcher = scripts / "Invoke.ps1"
        launcher.write_text(launcher.read_text().replace("{960}else{7200}", "{960}else{2}"))
    if fail_at == "uncooperative":
        guard = scripts / "ActiveScreenGuard.cs"
        text = guard.read_text().replace(
            'public string Failure="";',
            """
 public string Failure { get { return System.IO.File.Exists("PLACEHOLDER")
 ? "simulated_environment_failure" : ""; } }""",
        )
        text = text.replace("PLACEHOLDER", str(root / "child-pid.txt").replace("\\", "\\\\"))
        guard.write_text(text)
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    module = root / "benchmarks"
    module.mkdir()
    (module / "__init__.py").write_text("")
    (module / "active_screen_controls.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "for n in range(1,6):\n"
        " with Path('sequence.txt').open('a') as f: f.write(str(n)+'\\n')\n"
        f" if n=={fail_at}: sys.exit(2)\n"
    )
    if fail_at in {"uncooperative", "deadline"}:
        (module / "active_screen_controls.py").write_text(
            "import os,time\nfrom pathlib import Path\n"
            "Path('child-parent.txt').write_text(str(os.getppid()))\n"
            "Path('child-pid.txt').write_text(str(os.getpid()))\ntime.sleep(60)\n"
        )
    release = module / "results/active-screen-release-01"
    release.mkdir(parents=True)
    (release / "ready.json").write_text("{}")
    args = [str(PWSH), "-NoProfile", "-File", str(scripts / "Invoke.ps1")]
    if fail_at == "idle":
        launcher = scripts / "Invoke.ps1"
        text = launcher.read_text()
        assert text.count("{960}else{7200}") == 1
        launcher.write_text(text.replace("{960}else{7200}", "{1}else{7200}"))
        result = subprocess.run(
            [*args, "-Mode", "IdleCheck", "-IdleAttempt", "2"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads((module / "results/active-screen-idle-02/result.json").read_text())
        assert report["load_executed"] is False and report["child_shutdown"] is None
        assert report["released"] is True
        assert not (root / "sequence.txt").exists()
        assert not (module / "results/active-screen-idle-01").exists()
        assert not (module / "results/active-screen-operation-01").exists()
        rejected = subprocess.run(
            [*args, "-Mode", "Execute", "-IdleAttempt", "2"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert rejected.returncode == 2
        assert "valid only for IdleCheck" in rejected.stderr
        assert not (module / "results/active-screen-operation-01").exists()
        return
    if fail_at in {"uncooperative", "deadline"}:
        try:
            # Files avoid waiting for EOF on pipes inherited by the surviving child.
            with (tmp_path / "output.txt").open("w") as stream:
                result = subprocess.run(
                    args, cwd=tmp_path, stdout=stream, stderr=stream, timeout=15
                )
            assert result.returncode == 2
            report = json.loads(
                (module / "results/active-screen-operation-01/result.json").read_text()
            )
            assert report["released"] is True
            assert report["child_shutdown"]["exited"] is False
            assert report["child_shutdown"]["condition"] == "child_shutdown_timeout"
            assert report["child_shutdown"]["forced_termination"] is False
            # Windows venv python.exe is a redirector; the tracked Process is its parent.
            assert report["child_shutdown"]["pid"] == int((root / "child-parent.txt").read_text())
            assert "Manual inspection required" in (tmp_path / "output.txt").read_text()
        finally:
            # Only the exact synthetic child created by this test, never host processes.
            if (root / "child-pid.txt").exists():
                os.kill(int((root / "child-pid.txt").read_text()), signal.SIGTERM)
        return
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == (2 if fail_at else 0), result.stderr
    assert (root / "sequence.txt").read_text().splitlines() == [
        str(n) for n in range(1, (fail_at or 5) + 1)
    ]
    report = module / "results/active-screen-operation-01/result.json"
    before = report.read_bytes()
    assert json.loads(before.decode("utf-8-sig"))["released"] is True
    repeated = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert repeated.returncode == 2
    assert report.read_bytes() == before

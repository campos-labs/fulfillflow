"""Continuation uses real metadata contracts and safe child processes, never benchmark load."""

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
from benchmarks import comparison_continuation as continuation
from benchmarks import comparison_controls as comparison
from benchmarks import paired_controls as controls
from benchmarks import run_campaign as runner
from benchmarks.operational_errors import DiagnosticExportError, error_report
from benchmarks.sensitivity_controls import PWSH
from tests.unit.test_comparison120 import configured  # noqa: F401


@pytest.fixture
def segment(configured, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(controls, "STEPS", tuple(replace(s, continuation=True) for s in configured))
    monkeypatch.setattr(continuation, "PACKAGE", controls.PACKAGE)
    monkeypatch.setattr(continuation, "PRESERVED", tmp_path / "old/mixed-4-users-r01")
    return controls.STEPS


@pytest.mark.parametrize("failed_at", [None, 1, 4, 5, 59])
def test_all_59_children_in_fixed_order_and_first_failure_stops(segment, monkeypatch, failed_at):
    controls.write_report(controls.PACKAGE / "review.json", {"launcher": {}})
    controls._write_checksums(controls.PACKAGE)
    monkeypatch.setattr(continuation, "verify_release", lambda: {})
    monkeypatch.setattr(continuation, "verify_history", lambda: {"immutable": True})
    monkeypatch.setattr(continuation, "retire_original_resources", lambda: None)
    monkeypatch.setattr(controls, "assert_projects_absent", lambda: None)
    monkeypatch.setattr(
        continuation,
        "load_comparison_campaign",
        lambda _: SimpleNamespace(manifest=SimpleNamespace(host=None)),
    )
    monkeypatch.setattr(
        continuation,
        "HostProbe",
        lambda *_a, **_k: SimpleNamespace(identity=lambda: {}, dynamic=lambda _: {}),
    )
    monkeypatch.setattr(comparison, "verify_block", lambda _: None)
    calls = []
    old = continuation.PRESERVED / "unchanged.txt"
    old.parent.mkdir(parents=True)
    old.write_text("original")

    def run(step, _launcher):
        step.attempt.mkdir()
        for repetition in range(step.first_repetition, 6):
            calls.append((step.number, repetition))
            code = 2 if len(calls) == failed_at else 0
            result = subprocess.run(
                [sys.executable, "-c", f"raise SystemExit({code})"], timeout=10, check=False
            )
            controls.write_report(
                step.attempt / f"r{repetition:02d}.json", {"exit_code": result.returncode}
            )
            if result.returncode:
                controls._write_checksums(step.attempt)
                return result.returncode
        controls._write_checksums(step.attempt)
        return 0

    monkeypatch.setattr(controls, "run_step", run)
    assert continuation.execute({}) == (2 if failed_at else 0)
    expected = [(s.number, n) for s in segment for n in range(s.first_repetition, 6)]
    assert len(expected) == 59
    assert calls == expected[:failed_at] if failed_at else calls == expected
    assert old.read_text() == "original"
    controls.verify_checksums(controls.JOURNAL)
    with pytest.raises(controls.ControlError, match="already exist"):
        continuation.execute({})
    assert len(calls) == (failed_at or 59)


def test_export_failure_retains_all_branches_in_stderr(tmp_path, monkeypatch, capsys):
    class Process:
        def wait(self, _):
            return 2

        def ensure_stopped(self):
            raise runner.CampaignExecutionError("teardown token=hidden")

    monkeypatch.setattr(runner, "ManagedProcess", lambda *_a, **_k: Process())
    monkeypatch.setattr(
        runner, "_write_json", lambda *_: (_ for _ in ()).throw(OSError("export token=hidden"))
    )
    with pytest.raises(DiagnosticExportError) as caught:
        runner._run_preparation(["simulated"], 120, tmp_path / "error.json")
    report = error_report(caught.value)["errors"][0]["diagnostic"]
    assert report["process_returncode"] == 2
    assert report["primary"]["errors"][0]["type"] == "CampaignExecutionError"
    assert report["shutdown"]["errors"][0]["type"] == "CampaignExecutionError"
    assert report["export"]["errors"][0]["type"] == "OSError"
    output = capsys.readouterr().err
    assert "hidden" not in output
    assert json.loads(output)["errors"][0]["diagnostic"] == report


def test_runner_segment_rejects_wrong_manifest_or_destination(segment, monkeypatch, tmp_path):
    monkeypatch.setattr(continuation, "verify_release", lambda: {})
    first = segment[0]
    assert continuation.runner_segment(comparison.executable(first), first.attempt / "run") == [
        continuation.PRESERVED
    ]
    with pytest.raises(controls.ControlError, match="destination or manifest"):
        continuation.runner_segment(comparison.executable(first), tmp_path / "wrong")
    with pytest.raises(controls.ControlError, match="destination or manifest"):
        continuation.runner_segment(first.candidate, first.attempt / "run")
    second = segment[1]
    assert continuation.runner_segment(comparison.executable(second), second.attempt / "run") == []


def test_history_cannot_be_resealed_to_change_approved_evidence(segment, monkeypatch, tmp_path):
    monkeypatch.setattr(continuation, "RESULTS", tmp_path)
    monkeypatch.setattr(continuation, "HISTORY", {"preserved": "0" * 64})
    controls.write_report(tmp_path / "preserved/metadata.json", {"valid": True})
    controls._write_checksums(tmp_path / "preserved")
    with pytest.raises(controls.ControlError, match="preserved campaign identity"):
        continuation.verify_history()


@pytest.fixture(scope="module")
def fake_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("continuation") / "repo with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "benchmarks").mkdir()
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    shutil.copy("scripts/Invoke-Comparison120Continuation.ps1", root / "scripts")
    (root / "benchmarks/__init__.py").touch()
    (root / "benchmarks/comparison_continuation.py").write_text(
        "import sys,os,json,subprocess\nfrom pathlib import Path\n"
        "launcher=json.load(sys.stdin)\n"
        "code=int(os.environ['SIM_CODE'])\n"
        "events=[]\n"
        "for n in range(1,60):\n"
        " child=subprocess.run([sys.executable,'-c','raise SystemExit('+str(code)+')'])\n"
        " events.append(n)\n"
        " if child.returncode: break\n"
        "Path(os.environ['SIM_LOG']).write_text(json.dumps("
        "{'launcher':launcher,'events':events,'args':sys.argv[1:]}))\n"
        "sys.exit(code)\n"
    )
    return root


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows PowerShell required")
@pytest.mark.parametrize("code", [0, 2, 130])
def test_real_powershell_59_simulated_children_and_exit(fake_repo, tmp_path, code):
    log = tmp_path / "events.json"
    env = {**os.environ, "SIM_CODE": str(code), "SIM_LOG": str(log)}
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(PWSH),
            "-NoProfile",
            "-File",
            str(fake_repo / "scripts/Invoke-Comparison120Continuation.ps1"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == code
    data = json.loads(log.read_text())
    assert data["events"] == list(range(1, 60)) if code == 0 else data["events"] == [1]
    assert data["args"] == ["--execute"]
    assert Path(data["launcher"]["executable"]).resolve() == PWSH.resolve()


def test_runner_executes_four_new_repetitions_and_summarizes_external_first(
    segment, monkeypatch, tmp_path
):
    from datetime import UTC, datetime

    from benchmarks.campaign import load_campaign
    from benchmarks.comparison_protocol import ComparisonManifest

    step = segment[0]
    bundle = replace(
        load_campaign(Path("benchmarks/fixtures/smoke-campaign.json")),
        manifest=ComparisonManifest.model_validate(controls.read_json(comparison.executable(step))),
    )
    root = tmp_path / "application"
    root.mkdir()
    (root / "uv.lock").write_text("frozen")
    git = runner.GitProvenance(bundle.manifest.git_sha, "", True, True)
    snapshot = SimpleNamespace(label="synthetic", metrics=lambda: {})
    db = SimpleNamespace(snapshot=lambda _: snapshot)
    monkeypatch.setattr(runner, "run_capture", lambda *_: SimpleNamespace(stdout=str(root)))
    monkeypatch.setattr(runner, "_git_provenance", lambda _: git)
    monkeypatch.setattr(runner, "_project_release", lambda _: bundle.manifest.release)
    monkeypatch.setattr(runner, "runner_provenance", lambda *_a, **_k: {"new": True})
    monkeypatch.setattr(continuation, "verify_release", lambda: {})
    monkeypatch.setattr(
        runner,
        "HostProbe",
        lambda *_a, **_k: SimpleNamespace(identity=lambda: {}, dynamic=lambda _: {}),
    )
    observed = SimpleNamespace(
        container_ids={"postgres": "p", "loadgen": "l"},
        postgres_user="u",
        postgres_database="d",
        checks={},
    )
    monkeypatch.setattr(runner, "DockerProbe", lambda *_: SimpleNamespace(observe=lambda: observed))
    monkeypatch.setattr(runner, "DatabaseProbe", lambda *_a, **_k: db)
    for name in (
        "_install_runtime_manifest",
        "_stabilize",
        "_verify_measurement",
        "write_database_counts",
        "_merge_operational_results",
        "_require_repetition_artifacts",
    ):
        monkeypatch.setattr(runner, name, lambda *_: None)
    monkeypatch.setattr(runner, "_verify_initial_state", lambda *_: (snapshot, git))
    monkeypatch.setattr(runner, "_verify_warmup", lambda *_: git)

    stats = (
        "Name,Request Count,Failure Count,Requests/s,Median Response Time,95%\n"
        "Aggregated,100,0,10,2,3\n"
    )
    continuation.PRESERVED.mkdir(parents=True)
    (continuation.PRESERVED / "locust_stats.csv").write_text(stats)
    controls._write_checksums(continuation.PRESERVED)
    digest = controls._sha256(continuation.PRESERVED / "checksums.sha256")
    calls = []

    def preparation(_command, deadline, path):
        assert deadline == 120
        calls.append(path.parent.name)
        # Simulate a successful preparation subprocess without PostgreSQL or Docker.
        result = subprocess.run([sys.executable, "-c", "raise SystemExit(0)"], timeout=10)
        assert result.returncode == 0

    def phase(*args):
        directory = args[-1]
        if args[2] == "warmup":
            controls.write_report(
                directory / "warmup/warmup-progress.json",
                {
                    "schema_version": 1,
                    "admission_seconds": 120,
                    "registered_users": 4,
                    "in_flight": 0,
                    "failure_code": None,
                    "warmup_complete": True,
                    "applied_by_user": [430] * 4,
                },
            )
        else:
            (directory / "locust_stats.csv").write_text(stats)
        return SimpleNamespace(
            phase=args[2], started_at=datetime.now(UTC), finished_at=datetime.now(UTC), returncode=0
        )

    monkeypatch.setattr(runner, "_run_preparation", preparation)
    monkeypatch.setattr(runner, "_run_phase", phase)
    target = step.attempt / "run"
    assert (
        runner._execute(
            bundle,
            comparison.executable(step),
            "http://simulated",
            target,
            ["simulated"],
            application_source=root,
            comparison_continuation=True,
        )
        == 0
    )
    assert calls == [f"mixed-4-users-r{n:02d}.partial" for n in range(2, 6)]
    assert not (target / "mixed-4-users-r01").exists()
    assert (target / "summary.csv").exists()
    assert not (target / ".incomplete.json").exists()
    assert controls._sha256(continuation.PRESERVED / "checksums.sha256") == digest
    for n in range(2, 6):
        metadata = controls.read_json(target / f"mixed-4-users-r{n:02d}/metadata.json")
        assert metadata["repetition"] == n
        assert "loads" not in metadata["protocol_expected"]
    link = controls.read_json(target / "continuation.json")
    assert link["preserved"] == [{"path": str(continuation.PRESERVED), "checksums_sha256": digest}]


def test_original_cleanup_refuses_foreign_container(segment, monkeypatch, tmp_path):
    monkeypatch.setattr(controls, "verify_source", lambda _: None)
    old = tmp_path / "original"
    monkeypatch.setattr(continuation, "ORIGINAL_ATTEMPT", old)
    controls.write_report(
        old / "preparation/owned.json",
        {"project": "fulfillflow-comparison120-win9445-01-v10", "source": str(segment[0].source)},
    )
    path = old / "diagnostics/containers.txt"
    path.parent.mkdir()
    path.write_text(json.dumps({"ID": "a" * 12}))
    monkeypatch.setattr(continuation, "_run", lambda *_a, **_k: SimpleNamespace(stdout="b" * 64))
    with pytest.raises(controls.ControlError, match="containers differ"):
        continuation.original_resources()


def test_first_segment_preparation_starts_at_two_without_old_cleanup(segment, monkeypatch):
    step = segment[0]
    events = []

    def refuse(_):
        events.append("new-preparation")
        raise controls.ControlError("simulated stop before Docker")

    monkeypatch.setattr(controls, "verify_source", refuse)
    monkeypatch.setattr(controls, "cleanup", lambda *_: pytest.fail("no previous new repetition"))
    assert controls.prepare_step(step, setup_only=False) == 2
    assert events == ["new-preparation"]
    assert (step.attempt / "preparation/r02/error.json").is_file()
    assert not (step.attempt / "preparation/r01").exists()


@pytest.mark.parametrize("defect", ["source", "plan", "preserved", "coordinator"])
def test_continuation_review_rejects_identity_drift(segment, monkeypatch, defect):
    draft = {
        "continuation_contract": continuation.CONTRACT,
        "inventory": continuation.inventory(),
        "preserved": {"immutable": True},
        "coordinator": {"components": {"script": "hash"}},
    }
    monkeypatch.setattr(continuation, "verify_history", lambda: {"immutable": True})
    monkeypatch.setattr(
        continuation, "coordinator_identity", lambda: {"components": {"script": "hash"}}
    )
    if defect == "plan":
        draft["inventory"] = []
    elif defect == "preserved":
        draft["preserved"] = {}
    elif defect == "coordinator":
        draft["coordinator"] = {}

    def verified_draft():
        if defect == "source":
            raise controls.ControlError("reviewed comparison source or protocol changed")
        return draft

    monkeypatch.setattr(comparison, "verify_draft", verified_draft)
    with pytest.raises(controls.ControlError):
        continuation.verify_review()


def test_continuation_prepare_argv_is_explicit(segment):
    assert "benchmarks.comparison_continuation" in controls.preparation_argv(segment[0])
    assert segment[0].first_repetition == 2
    assert all(s.first_repetition == 1 for s in segment[1:])
    assert len({s.attempt for s in segment}) == 12


def test_changed_application_source_blocks_before_cleanup_commands(segment, monkeypatch):
    monkeypatch.setattr(continuation, "verify_history", lambda: {})
    monkeypatch.setattr(
        controls,
        "verify_source",
        lambda _: (_ for _ in ()).throw(controls.ControlError("measured source contains changes")),
    )
    monkeypatch.setattr(
        continuation,
        "_run",
        lambda *_a, **_k: pytest.fail("no Docker command before source validation"),
    )
    with pytest.raises(controls.ControlError, match="measured source"):
        continuation.retire_original_resources()

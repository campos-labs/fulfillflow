"""One diagnostic preserves frozen warm-up gates and cannot enter measurement."""

import copy
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks import paired_controls as controls
from benchmarks import run_campaign as runner
from benchmarks.campaign import CampaignManifest, load_campaign
from benchmarks.collectors import DatabaseSnapshot, ObservedEnvironment
from benchmarks.phase_diagnostics import warmup_quota_evidence
from tests.unit.test_reviewed_controls import originals


@pytest.fixture
def warmup_series(tmp_path, monkeypatch):
    for name in ("SERIES", "PACKAGE", "JOURNAL", "PROJECTS", "STEPS"):
        monkeypatch.setattr(controls, name, getattr(controls, name))
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    controls.configure_series("warmup")
    step = controls.STEPS[0]
    controls.write_report(step.candidate, controls.candidate_document(step, originals()))
    return step


def diagnostic_bundle():
    bundle = load_campaign(Path("benchmarks/fixtures/smoke-campaign.json"))
    doc = originals()["v11"]
    doc.update(official=False, repetitions=1, profile="mixed")
    reference = originals()["v10"]
    doc["weights"] = reference["weights"]
    doc["loads"] = [load for load in reference["loads"] if load["users"] == 12]
    return replace(bundle, manifest=CampaignManifest.model_validate(doc))


def test_single_diagnostic_keeps_frozen_contract_and_distinct_destinations(warmup_series):
    step = warmup_series
    candidate = controls.read_json(step.candidate)
    manifest = CampaignManifest.model_validate(candidate)
    runner._validate_warmup_diagnostic(SimpleNamespace(manifest=manifest))
    assert len(controls.STEPS) == 1
    assert step.source.name == "comparison-win9445-v11-source-01"
    assert controls.PROJECTS == {"v11": "fulfillflow-ii-warmup12-win9445-01"}
    assert controls.warmup_interpretation()["attempts"] == 1
    original = copy.deepcopy(originals()["v11"])
    for field in ("images", "resources", "timeouts", "database", "warmup_quota_per_shipment"):
        assert candidate[field] == original[field]
    assert manifest.host.identity.os_build == "26200.9445"
    step.attempt.mkdir()
    with pytest.raises(controls.ControlError, match="no retry"):
        controls.require_new_execution()


@pytest.mark.parametrize(
    "change",
    [
        {"official": True},
        {"release": "v1.0.0"},
        {"repetitions": 5},
        {"profile": "timeline"},
        {"loads": []},
        {"warmup_quota_per_shipment": 1},
    ],
)
def test_diagnostic_refuses_other_contracts_before_preparation(tmp_path, monkeypatch, change):
    bundle = diagnostic_bundle()
    bundle = replace(bundle, manifest=bundle.manifest.model_copy(update=change))
    monkeypatch.setattr(runner, "_run_preparation", lambda *_: pytest.fail("must refuse first"))
    with pytest.raises(runner.CampaignExecutionError, match="diagnostic requires"):
        runner._execute(
            bundle,
            Path("unused"),
            "http://unused",
            tmp_path / "out",
            [],
            diagnostic_warmup_only=True,
        )
    assert not (tmp_path / "out").exists()


def write_final(directory, applied=5160, *, promoted=False):
    directory.mkdir(parents=True, exist_ok=True)
    name = "locust_stats.csv" if promoted else "locust_final_stats.csv"
    (directory / name).write_text(
        f"Name,Request Count,Failure Count\nAggregated,{applied},0\n", encoding="utf-8"
    )
    (directory / "operational_results.http.csv").write_text(
        f"result,count\nAPPLIED,{applied}\n", encoding="utf-8"
    )


def test_quota_is_artifact_inference_and_never_internal_locust_reason(tmp_path):
    write_final(tmp_path, 3722)
    report = warmup_quota_evidence(tmp_path, 5160)
    assert report["missing_applied"] == 1438
    assert report["quota_incomplete"] is True
    assert report["basis"] == "derived_from_exported_artifacts"
    assert report["locust_internal_reason"] is None
    (tmp_path / "locust_final_stats.csv").unlink()
    assert warmup_quota_evidence(tmp_path, 5160)["available"] is False


@pytest.mark.parametrize("drift", [None, "coordinator", "runner", "candidate", "package"])
def test_warmup_package_refuses_identity_drift(warmup_series, monkeypatch, drift):
    identity = {"components": {"launcher": "frozen"}}
    monkeypatch.setattr(controls, "runner_provenance", lambda _: {"git": "runner"})
    monkeypatch.setattr(controls, "coordinator_identity", lambda: identity)
    monkeypatch.setattr(controls, "fingerprints", lambda: {"candidate": "frozen"})
    monkeypatch.setattr(controls, "verify_source", lambda *_: None)
    monkeypatch.setattr(controls, "sync_source", lambda *_a, **_k: None)
    monkeypatch.setattr(controls, "original_documents", originals)
    ready = {
        "order": [warmup_series.name],
        "fingerprints": controls.fingerprints(),
        "host_runner": {"git": "wrong" if drift == "runner" else "runner"},
        "coordinator": {} if drift == "coordinator" else identity,
    }
    controls.write_report(controls.PACKAGE / "ready.json", ready)
    if drift == "candidate":
        doc = controls.read_json(warmup_series.candidate)
        doc["images"]["core"] = "sha256:" + "0" * 64
        controls.write_report(warmup_series.candidate, doc)
    controls._write_checksums(controls.PACKAGE)
    if drift == "package":
        (controls.PACKAGE / "ready.json").write_text("{}")
    if drift:
        with pytest.raises(controls.ControlError):
            controls.prepared()
    else:
        controls.prepared()


def test_warmup_coordinator_sends_one_explicit_diagnostic_and_stops(warmup_series, monkeypatch):
    step = warmup_series
    for name in ("verify_source", "verify_candidates", "assert_projects_absent"):
        monkeypatch.setattr(controls, name, lambda *_: None)
    monkeypatch.setattr(controls, "coordinator_identity", lambda: {"git": "coordinator"})
    monkeypatch.setattr(controls, "image_preflight", lambda *_: {})
    monkeypatch.setattr(controls, "frozen_preflight", lambda *_: {})
    monkeypatch.setattr(controls, "environment_for", lambda *_: {})
    calls = []

    def run(argv, *_):
        calls.append(argv)
        assert "--diagnostic-warmup-only" in argv
        assert argv[argv.index("--application-source") + 1] == str(step.source)
        return subprocess.CompletedProcess(argv, 2)

    monkeypatch.setattr(controls, "_run_runner", run)
    assert controls.run_step(step, {}) == 2
    assert len(calls) == 1
    assert controls.read_json(step.attempt / "result.json")["mode"] == "diagnostic_warmup_only"
    with pytest.raises(controls.ControlError, match="no retry"):
        controls.require_new_execution()


@pytest.mark.parametrize("failure", [None, "warmup", "verification", "host"])
def test_execute_diagnostic_runs_same_gates_but_never_measurement(tmp_path, monkeypatch, failure):
    bundle = diagnostic_bundle()
    order = []
    identity = runner.GitProvenance(bundle.manifest.git_sha, "", True, True)
    observed = ObservedEnvironment(
        {},
        {"postgres": "db", "core": "core", "tracking": "tracking", "loadgen": "loadgen"},
        "unused",
        "unused",
    )
    monkeypatch.setattr(runner, "_git_provenance", lambda _: identity)
    monkeypatch.setattr(runner, "_project_release", lambda _: "v1.1.0")
    monkeypatch.setattr(runner, "DockerProbe", lambda *_: SimpleNamespace(observe=lambda: observed))

    class Database:
        def __init__(self, *_a, **_k):
            pass

        def snapshot(self, label):
            return DatabaseSnapshot(label, {}, {}, {})

        def reconcile(self):
            order.append("reconcile")
            return {"valid": True}

    monkeypatch.setattr(runner, "SplitDatabaseProbe", Database)
    monkeypatch.setattr(runner, "DatabaseProbe", Database)

    def dynamic(*_):
        order.append("host")
        if failure == "host":
            raise runner.EnvironmentMismatchError({"os_build": {"matches": False}})
        return {}

    monkeypatch.setattr(
        runner, "HostProbe", lambda *_a, **_k: SimpleNamespace(identity=lambda: {}, dynamic=dynamic)
    )
    monkeypatch.setattr(runner, "_run_preparation", lambda *_: order.append("prepare"))

    def initial(*_):
        order.append("initial")
        return DatabaseSnapshot("initial", {}, {}, {}), identity

    monkeypatch.setattr(runner, "_verify_initial_state", initial)
    monkeypatch.setattr(runner, "_install_runtime_manifest", lambda *_: "runtime")

    def stabilize(seconds):
        assert seconds == 300
        order.append("stabilize")
        return {}

    monkeypatch.setattr(runner, "_stabilize", stabilize)

    def phase(*args):
        assert args[2] == "warmup", "measurement is forbidden"
        order.append("warmup")
        if failure == "warmup":
            raise runner.CampaignExecutionError("Locust invalidated")
        directory = args[-1] / "warmup"
        write_final(directory, promoted=True)
        for name in (
            "locust_stats_history.csv",
            "locust_failures.csv",
            "locust_exceptions.csv",
            "response_codes.csv",
            "resources.csv",
            "resources.application.csv",
        ):
            (directory / name).touch()
        now = datetime.now(UTC)
        return runner.PhaseExecution("warmup", now, now, 0)

    monkeypatch.setattr(runner, "_run_phase", phase)

    def verify(*_):
        order.append("verify_warmup")
        if failure == "verification":
            raise runner.CampaignExecutionError("quota failed")
        return identity

    monkeypatch.setattr(runner, "_verify_warmup", verify)
    directory = tmp_path / "run"
    if failure:
        with pytest.raises((runner.CampaignExecutionError, runner.EnvironmentMismatchError)):
            runner._execute(
                bundle,
                Path("uv.lock"),
                "http://unused",
                directory,
                ["safe"],
                diagnostic_warmup_only=True,
            )
        assert (directory / ".incomplete.json").exists()
        assert not (directory / "warmup-diagnostic").exists()
    else:
        assert (
            runner._execute(
                bundle,
                Path("uv.lock"),
                "http://unused",
                directory,
                ["safe"],
                diagnostic_warmup_only=True,
            )
            == 0
        )
        assert order == [
            "prepare",
            "initial",
            "stabilize",
            "host",
            "warmup",
            "verify_warmup",
            "reconcile",
        ]
        assert controls.verify_warmup_result(directory)
        assert not list(directory.rglob(".incomplete.json"))


@pytest.mark.parametrize(
    "kind", ["exit", "collector", "timeout", "shutdown", "export", "completion", "start"]
)
def test_phase_reports_failure_owner_and_retains_partial_export(tmp_path, monkeypatch, kind):
    bundle = load_campaign(Path("benchmarks/fixtures/smoke-campaign.json"))
    exported = []
    original = (
        runner.CollectionSupervisionError("private")
        if kind == "collector"
        else runner.ProcessTimeoutError("private")
    )

    def wait(*_a, **_k):
        if kind in {"collector", "timeout"}:
            raise original
        return 2 if kind == "exit" else 0

    def stopped():
        if kind == "shutdown":
            raise OSError("private")

    def process(*_):
        if kind == "start":
            raise OSError("private")
        return SimpleNamespace(wait=wait, ensure_stopped=stopped)

    def marker(*args):
        if kind == "completion" and "completed" in args[1]:
            raise runner.CampaignExecutionError("private")

    def export(argv, *_):
        destination = Path(argv[-1])
        exported.append(destination.name)
        if kind == "export" and destination.name != "failed-runtime":
            raise OSError("private")
        write_final(destination, 3722)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(runner, "ManagedProcess", process)
    monkeypatch.setattr(
        runner,
        "ResourceSampler",
        lambda *_a, **_k: SimpleNamespace(
            start=lambda: None,
            stop=lambda: None,
            failure={"stage": "connections"} if kind == "collector" else None,
        ),
    )
    monkeypatch.setattr(runner, "_wait_for_container_file", marker)
    monkeypatch.setattr(runner, "_terminate_container_phase", lambda *_: None)
    monkeypatch.setattr(runner, "run_capture", export)
    observed = ObservedEnvironment({}, {"loadgen": "id"}, "unused", "unused")
    with pytest.raises((runner.CampaignExecutionError, runner.ExternalCommandError, OSError)):
        runner._run_phase(
            bundle,
            bundle.manifest.loads[0],
            "warmup",
            "http://unused",
            observed,
            object(),
            "runtime",
            tmp_path,
        )
    report = controls.read_json(tmp_path / "warmup/phase-error.json")
    expected = {"exit": "process_exit", "timeout": "process_timeout", "start": "process_start"}.get(
        kind, kind
    )
    assert report["stage"] == expected
    assert report["process_returncode"] == (
        2 if kind == "exit" else None if kind in {"start", "timeout", "collector"} else 0
    )
    assert "failed-runtime" in exported
    assert report["locust_internal_reason"] is None
    assert "private" not in json.dumps(report)
    if kind == "exit":
        assert report["collector"] is None
        assert "collection failed" not in report["message"]

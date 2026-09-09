"""No-load simulations of the operational entry, failures and exact argv transport."""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks import pilot_v11 as pilot
from benchmarks import prepare_v11 as prepare
from benchmarks.collectors import ExternalCommandError, run_capture
from benchmarks.operational_errors import error_report, write_report
from benchmarks.run_campaign import GitProvenance
from tests.unit.test_benchmark_v11 import candidate_payload


@pytest.fixture
def candidate(tmp_path):
    path = tmp_path / "review with spaces" / "candidate.json"
    document = candidate_payload()
    document["environment"].update(
        compose_project="fulfillflow-ii-pilot-simulation", compose_file=prepare.COMPOSE.name
    )
    document["cohorts"]["dataset_manifest"] = str(
        (pilot.ROOT / "benchmarks/datasets/benchmark-v1.0.json").resolve()
    )
    write_report(path, document)
    return path


def test_derivation_changes_only_authorized_selection_and_path(candidate, tmp_path):
    original = json.loads(candidate.read_text())
    result = pilot.pilot_document(candidate, tmp_path / "attempt")
    assert result["loads"] == [original["loads"][0]]
    assert result["official"] is False and result["repetitions"] == 1
    assert result["warmup_quota_per_shipment"] == 430
    for key in original.keys() - {"name", "loads", "official", "repetitions", "cohorts"}:
        assert result[key] == original[key]


@pytest.mark.parametrize(
    "field,value", [("warmup_quota_per_shipment", 432), ("stabilization_seconds", 0)]
)
def test_refuse_candidate_parameter_drift(candidate, tmp_path, field, value):
    document = json.loads(candidate.read_text())
    document[field] = value
    write_report(candidate, document)
    with pytest.raises(ValueError):
        pilot.pilot_document(candidate, tmp_path)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (None, 0),
        ("preflight", 2),
        ("runner", 2),
        ("interrupt", 130),
        ("cleanup", 2),
        ("diagnostics", 2),
    ],
)
def test_chain_stops_once_and_preserves_original_before_cleanup(
    candidate, tmp_path, monkeypatch, failure, expected
):
    events = []
    destination = tmp_path / "attempt"
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    # pilot_document needs the real frozen baseline, independently of isolated markers.
    derive = pilot.pilot_document
    real_root = prepare.ROOT

    def document(*args):
        monkeypatch.setattr(pilot, "ROOT", real_root)
        try:
            return derive(*args)
        finally:
            monkeypatch.setattr(pilot, "ROOT", tmp_path)

    monkeypatch.setattr(pilot, "pilot_document", document)
    monkeypatch.setenv("SESSION_SECRET", "sensitive-original")
    monkeypatch.setenv("BENCH_CORE_IMAGE", "preserved-image")

    def preflight(*args):
        events.append("preflight")
        if failure == "preflight":
            raise ValueError("blocked host")
        return {"verified": True}

    def execute(bundle, manifest, url, results, argv):
        events.append("runner")
        assert bundle.manifest.repetitions == 1 and len(bundle.manifest.loads) == 1
        assert runner_parse(json.dumps(argv)) == argv
        results.mkdir()
        (results / "preserved.txt").write_text("diagnostic evidence")
        marker = tmp_path / "benchmarks/results/preparation/fulfillflow-ii-pilot-simulation.json"
        write_report(marker, {})
        if failure == "interrupt":
            raise KeyboardInterrupt()
        if failure == "runner":
            raise ValueError("original sensitive-original failure")
        return 0

    def diagnostics(*args, **kwargs):
        events.append("diagnostics")
        assert (destination / "result.json").exists()
        if failure == "diagnostics":
            raise RuntimeError("export failed")

    def cleanup(*args):
        events.append("cleanup")
        assert events[-2] == "diagnostics"
        if failure in {"runner", "cleanup"}:
            raise RuntimeError("secondary cleanup failure")

    monkeypatch.setattr(pilot, "preflight", preflight)
    monkeypatch.setattr(pilot.runner, "_execute", execute)
    monkeypatch.setattr(pilot, "diagnostics", diagnostics)
    monkeypatch.setattr(pilot, "run_capture", cleanup)
    assert pilot.execute(candidate, destination, candidate, plan_only=False) == expected
    report = (destination / "result.json").read_text()
    assert "sensitive-original" not in report
    if failure == "runner":
        assert "original [redacted] failure" in report and "secondary cleanup failure" in report
    assert events.count("runner") == (0 if failure == "preflight" else 1)
    assert ("cleanup" in events) == (failure not in {"preflight", "diagnostics"})
    assert os.environ["BENCH_CORE_IMAGE"] == "preserved-image"
    with pytest.raises(ValueError, match="new"):
        pilot.execute(candidate, destination, candidate, plan_only=False)


def runner_parse(value):
    return pilot.runner._parse_command(value)


def test_original_process_failure_is_reported_without_argv_or_credentials(monkeypatch):
    monkeypatch.setenv("CORE_DB_PASSWORD", "sensitive-value")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv,
            7,
            "",
            "database rejected: sensitive-value\nraw_body=private-payload\nconnection refused",
        ),
    )
    with pytest.raises(ExternalCommandError) as captured:
        run_capture(["docker", "exec", "--password=argv-secret"], 1)
    report = error_report(captured.value)
    text = json.dumps(report)
    assert report["errors"][1]["returncode"] == 7
    assert "connection refused" in text
    assert all(value not in text for value in ("argv-secret", "sensitive-value", "private-payload"))


def test_json_stdin_preserves_unicode_quotes_and_backslashes(monkeypatch):
    options = {
        "candidate": 'C:\\revisão com espaços\\"candidate".json',
        "destination": "C:\\tentativa 'única'",
        "audit": "C:\\audit.json",
        "plan_only": True,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(options)))
    received = []
    monkeypatch.setattr(pilot, "execute", lambda *args, **kw: received.append((args, kw)) or 0)
    assert pilot.main() == 0
    assert str(received[0][0][0]) == options["candidate"]
    assert str(received[0][0][1]) == options["destination"]


@pytest.mark.parametrize("failure", ["dirty", "checksum", "preexisting"])
def test_preflight_blocks_before_images_or_host(candidate, tmp_path, monkeypatch, failure):
    document = pilot.pilot_document(candidate, tmp_path / "attempt")
    write_report(
        candidate.parent / "SHA256SUMS.json",
        {
            candidate.name: "invalid"
            if failure == "checksum"
            else pilot.runner._file_sha256(candidate)
        },
    )
    monkeypatch.setattr(
        pilot.runner,
        "_git_provenance",
        lambda _: GitProvenance(
            document["git_sha"], "codex/v1.1-tracking", failure != "dirty", True
        ),
    )
    calls = []

    def observe(argv, timeout):
        calls.append(argv)
        assert argv[:3] == ["docker", "ps", "-aq"]
        return subprocess.CompletedProcess(argv, 0, "preexisting-container", "")

    monkeypatch.setattr(pilot, "run_capture", observe)
    with pytest.raises(ValueError):
        pilot.preflight(candidate, candidate, document)
    assert len(calls) == (1 if failure == "preexisting" else 0)


def test_prepare_original_error_survives_cleanup_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "ROOT", tmp_path)
    evidence = tmp_path / "evidence"
    monkeypatch.setenv("FULFILLFLOW_PREPARATION_EVIDENCE", str(evidence))
    for owner in ("core", "tracking"):
        monkeypatch.setenv(f"{owner.upper()}_DB_PASSWORD", "synthetic")
        monkeypatch.setenv(
            f"{owner.upper()}_DATABASE_URL",
            f"postgresql+psycopg://fulfillflow_{owner}:synthetic@db:5432/fulfillflow_{owner}",
        )
    events = []
    original = ExternalCommandError("original preparation failure")

    def command(argv, timeout):
        if "up" in argv:
            events.append("up")
            raise original
        if "down" in argv:
            events.append("down")
            if events.count("down") == 2:
                assert events[-2] == "diagnostics"
                raise ExternalCommandError("secondary cleanup failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(prepare, "run_capture", command)
    monkeypatch.setattr(prepare, "diagnostics", lambda *args: events.append("diagnostics"))
    with pytest.raises(ExternalCommandError) as captured:
        prepare.restore("fulfillflow-ii-simulation", "fulfillflow-ii-simulation")
    assert captured.value is original
    assert "original preparation failure" in (evidence / "error.json").read_text()
    assert "secondary cleanup failure" in (evidence / "cleanup-error.json").read_text()


def test_failed_phase_artifacts_export_before_cleanup(tmp_path, monkeypatch):
    from benchmarks import collectors, operational_errors

    calls = []

    def command(argv, timeout):
        calls.append(argv)
        stdout = "loadgen-id" if argv[-3:] == ["ps", "-q", "loadgen"] else ""
        if argv[:2] == ["docker", "exec"]:
            stdout = "True"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(collectors, "run_capture", command)
    operational_errors.diagnostics(["docker", "compose"], tmp_path / "diag", include_runtime=True)
    assert calls[-1][:3] == ["docker", "cp", "loadgen-id:/tmp/fulfillflow-benchmark/."]


def test_reuse_audit_refuses_wrong_image_without_reauditing(tmp_path, monkeypatch):
    report = tmp_path / "loadgen-compatibility.json"
    write_report(report, {"parent": prepare.FROZEN_LOADGEN, "candidate": "wrong"})
    write_report(tmp_path / "SHA256SUMS.json", {report.name: pilot.runner._file_sha256(report)})
    monkeypatch.setattr(prepare, "run_capture", lambda *args: pytest.fail("must not inspect image"))
    with pytest.raises(ValueError, match="approved derived image"):
        prepare.reuse_loadgen_audit("candidate", report)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell launcher uses the Windows venv")
def test_real_powershell_plan_transports_paths_and_exit_codes(candidate, tmp_path):
    pwsh = shutil.which("pwsh") or str(
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
    )
    if not Path(pwsh).is_file():
        pytest.skip("PowerShell 7 is not installed")
    destination = tmp_path / "tentativa com espaços e ação 'única'"
    argv = [
        pwsh,
        "-NoProfile",
        "-File",
        str(pilot.ROOT / "scripts/Invoke-V11Pilot.ps1"),
        "-Candidate",
        str(candidate),
        "-Destination",
        str(destination),
        "-Audit",
        str(candidate),
        "-PlanOnly",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["results_directory"] == str(destination / "run")
    assert runner_parse(json.dumps(plan["prepare_argv"])) == plan["prepare_argv"]
    assert not destination.exists()
    destination.mkdir()
    refused = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert refused.returncode == 2 and "destination must be new" in refused.stderr
    missing_env = subprocess.run(
        [*argv, "-EnvironmentFile", str(tmp_path / "missing.env")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert missing_env.returncode == 2
    assert json.loads(missing_env.stderr)["stage"] == "launcher"

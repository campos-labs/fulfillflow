"""No-load regressions for the approved four-run frozen comparison launcher."""

from __future__ import annotations

import copy
import io
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from benchmarks import paired_controls as controls
from tests.unit.test_benchmark_v11 import candidate_payload


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "PACKAGE", tmp_path / "package")
    monkeypatch.setattr(controls, "JOURNAL", tmp_path / "journal")
    return tmp_path


@pytest.fixture
def originals(monkeypatch):
    documents = {
        "v10": controls.read_json(controls.ROOT / "benchmarks/campaigns/v1-baseline-mixed.json"),
        "v11": candidate_payload(),
    }
    documents["v11"].update(git_sha=controls.REVISIONS["v11"], official=False)
    monkeypatch.setattr(controls, "original_documents", lambda: copy.deepcopy(documents))
    return documents


def materialize(originals):
    for step in controls.STEPS:
        controls.write_report(step.candidate, controls.candidate_document(step, originals))


@pytest.mark.parametrize("index", range(4))
def test_candidate_preserves_every_field_except_explicit_selection_and_isolation(
    isolated, originals, index
):
    step = controls.STEPS[index]
    original = copy.deepcopy(originals[step.version])
    document = controls.candidate_document(step, originals)
    assert document["name"] == step.name
    assert document["official"] is False
    assert document["repetitions"] == 1
    assert document["loads"] == [original["loads"][0]]
    assert document["host"]["identity"]["os_build"] == "26200.9445"
    dataset = (step.candidate.parent / document["cohorts"]["dataset_manifest"]).resolve()
    assert dataset == step.source / "benchmarks/datasets/benchmark-v1.0.json"
    if step.version == "v11":
        assert document["environment"]["compose_project"] == controls.PROJECTS["v11"]
        document["environment"]["compose_project"] = original["environment"]["compose_project"]
    for field in ("name", "official", "repetitions", "loads"):
        document[field] = original[field]
    document["host"]["identity"]["os_build"] = original["host"]["identity"]["os_build"]
    document["cohorts"]["dataset_manifest"] = original["cohorts"]["dataset_manifest"]
    assert document == original
    assert originals[step.version] == original


@pytest.mark.parametrize("field,value", [("warmup_quota_per_shipment", 431), ("official", True)])
def test_verification_rejects_candidate_drift(isolated, originals, field, value):
    materialize(originals)
    controls.verify_candidates()
    step = controls.STEPS[2]
    document = controls.read_json(step.candidate)
    document[field] = value
    controls.write_report(step.candidate, document)
    with pytest.raises(controls.ControlError, match="frozen authorized candidate"):
        controls.verify_candidates()


@pytest.mark.parametrize("existing", ["journal", "a1", "b1", "b2", "a2"])
def test_execution_refuses_any_preexisting_destination(isolated, existing):
    destination = (
        controls.JOURNAL
        if existing == "journal"
        else next(step.attempt for step in controls.STEPS if step.label == existing)
    )
    destination.mkdir()
    with pytest.raises(controls.ControlError, match="no retry or overwrite"):
        controls.require_new_execution()


def test_preparation_integrity_includes_ready_and_diagnostics(isolated, originals, monkeypatch):
    materialize(originals)
    monkeypatch.setattr(controls, "fingerprints", lambda: {"driver": "frozen"})
    monkeypatch.setattr(controls, "verify_source", lambda _step: None)
    monkeypatch.setattr(controls, "sync_source", lambda _step, **_kwargs: None)
    controls.write_report(
        controls.PACKAGE / "ready.json",
        {"fingerprints": {"driver": "frozen"}, "order": [step.name for step in controls.STEPS]},
    )
    controls._write_checksums(controls.PACKAGE)
    controls.prepared()
    controls.write_report(controls.PACKAGE / "setup-extra.json", {"changed": True})
    with pytest.raises(controls.ControlError, match="checksums differ"):
        controls.prepared()


@pytest.mark.parametrize("failed_index", [0, 1, 2, 3, None])
def test_fixed_order_stops_at_first_failure_and_preserves_journal(
    isolated, monkeypatch, failed_index
):
    controls.write_report(controls.PACKAGE / "ready.json", {})
    monkeypatch.setattr(controls, "prepared", lambda: None)
    monkeypatch.setattr(controls, "assert_projects_absent", lambda: None)
    visited = []

    def run(step, _launcher):
        visited.append((step.label, step.version, step.block))
        return 7 if step.number - 1 == failed_index else 0

    monkeypatch.setattr(controls, "run_step", run)
    assert controls.execute_block({}) == (0 if failed_index is None else 7)
    expected = [("a1", "v10", 1), ("b1", "v11", 1), ("b2", "v11", 2), ("a2", "v10", 2)]
    assert visited == expected if failed_index is None else visited == expected[: failed_index + 1]
    report = controls.read_json(controls.JOURNAL / "result.json")
    assert report["complete"] is (failed_index is None)
    assert len(report["completed"]) == (4 if failed_index is None else failed_index)
    assert (controls.JOURNAL / "checksums.sha256").is_file()


@pytest.mark.parametrize("index", [0, 1])
def test_preparation_uses_frozen_seed_and_probes_without_build_pull_or_load(
    isolated, originals, monkeypatch, index
):
    materialize(originals)
    step = controls.STEPS[index]
    monkeypatch.setattr(controls, "verify_source", lambda _step: None)
    monkeypatch.setattr(controls, "assert_projects_absent", lambda: None)
    monkeypatch.setattr(controls, "image_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "environment_for", lambda *_args: {})
    calls = []
    probes = []
    monkeypatch.setattr(controls, "_run", lambda argv, **_kwargs: calls.append(argv))
    monkeypatch.setattr(
        controls,
        "source_python",
        lambda _step, code, *_args, **_kwargs: (
            probes.append(code) or {"verified": True, "load_executed": False}
        ),
    )
    monkeypatch.setattr(controls, "_run_runner", lambda *_args: pytest.fail("load forbidden"))
    assert controls.prepare_step(step, setup_only=True) == 0
    assert all(
        "--execute" not in argv and "locust" not in argv and "build" not in argv for argv in calls
    )
    for argv in calls[1:]:
        assert argv[argv.index("--pull") + 1] == "never"
        if "up" in argv:
            assert "--no-build" in argv
    assert "_verify_initial_state(bundle, database)" in probes[0]
    assert "SplitDatabaseProbe" in probes[0]
    assert not step.attempt.exists()
    assert (controls.PACKAGE / "setup" / step.version / "preparation/ready.json").is_file()


def test_preexisting_project_is_not_claimed_or_cleaned(isolated, originals, monkeypatch):
    materialize(originals)
    monkeypatch.setattr(controls, "verify_source", lambda _step: None)

    def present():
        raise controls.ControlError("preserve existing project")

    monkeypatch.setattr(controls, "assert_projects_absent", present)
    monkeypatch.setattr(
        controls, "_run", lambda *_args, **_kwargs: pytest.fail("no Docker mutation")
    )
    assert controls.prepare_step(controls.STEPS[0], setup_only=True) == 2
    assert not list(controls.PACKAGE.rglob("owned.json"))


@pytest.mark.parametrize("fail_setup", [False, True])
def test_prepare_package_checks_each_version_without_creating_execution_destinations(
    isolated, originals, monkeypatch, fail_setup
):
    monkeypatch.setattr(controls, "verify_source", lambda _step: None)
    monkeypatch.setattr(controls, "sync_source", lambda _step: None)
    monkeypatch.setattr(controls, "assert_projects_absent", lambda: None)
    monkeypatch.setattr(controls, "image_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "frozen_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "source_python", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(controls, "fingerprints", lambda: {"driver": "frozen"})
    monkeypatch.setattr(controls, "_run_runner", lambda *_args: pytest.fail("load forbidden"))
    calls = []

    def run(argv, **_kwargs):
        assert "--execute" not in argv
        if "--prepare-step" in argv:
            label = argv[argv.index("--prepare-step") + 1]
            assert "--setup-only" in argv
            calls.append(label)
            if fail_setup:
                controls.write_report(controls.PACKAGE / "setup/v10/preparation/owned.json", {})
                raise controls.ControlError("primary timeout")
        return CompletedProcess(argv, 0, "", "")

    def diagnostics(_step, _destination, *, stop):
        if fail_setup:
            assert stop is True
            assert (controls.PACKAGE / "setup/v10/error.json").is_file()
            raise controls.ControlError("secondary diagnostic failure")
        assert stop is False

    monkeypatch.setattr(controls, "_run", run)
    monkeypatch.setattr(controls, "diagnostics", diagnostics)
    monkeypatch.setattr(
        controls, "cleanup", lambda step, _evidence: calls.append(f"clean-{step.label}")
    )
    if fail_setup:
        with pytest.raises(controls.ControlError, match="setup-without-load-v10"):
            controls.prepare_package({})
        assert calls == ["a1"]
        assert "primary timeout" in (controls.PACKAGE / "error.json").read_text()
        assert (controls.PACKAGE / "setup/v10/diagnostic-error.json").is_file()
        assert not (controls.PACKAGE / "ready.json").exists()
    else:
        assert controls.prepare_package({}) == 0
        assert calls == ["a1", "clean-a1", "b1", "clean-b1"]
        ready = controls.read_json(controls.PACKAGE / "ready.json")
        assert ready["database_setup_verified"] == ["v10", "v11"]
        assert ready["load_executed"] is False
        assert ready["interpretation"]["pairs"] == [["a1", "b1"], ["a2", "b2"]]
        assert ready["interpretation"]["new_practical_margin"] is None
    assert not controls.JOURNAL.exists()
    assert all(not step.attempt.exists() for step in controls.STEPS)


@pytest.mark.parametrize(
    "runner_code,diagnostic_error", [(7, False), (130, False), (0, True), (0, False)]
)
def test_runner_failure_and_diagnostic_failure_preserve_resources(
    isolated, originals, monkeypatch, runner_code, diagnostic_error
):
    materialize(originals)
    step = controls.STEPS[1]
    monkeypatch.setattr(controls, "verify_source", lambda _step: None)
    monkeypatch.setattr(controls, "assert_projects_absent", lambda: None)
    monkeypatch.setattr(controls, "image_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "environment_for", lambda *_args: {})
    monkeypatch.setattr(controls, "frozen_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "_source_python", lambda _source: Path(sys.executable))
    calls = []

    def runner(argv, source, _environment, _evidence):
        assert source == step.source
        assert argv[argv.index("--manifest") + 1] == str(step.candidate)
        controls.write_report(step.attempt / "preparation/owned.json", {})
        controls.write_report(step.attempt / "run/mixed-4-users-r01/metadata.json", {"valid": True})
        return CompletedProcess(argv, runner_code, "", "")

    def diagnostics(_step, _destination, *, stop):
        calls.append(("diagnostics", stop))
        assert (step.attempt / "result.json").is_file()
        if diagnostic_error:
            raise controls.ControlError("export failed")

    monkeypatch.setattr(controls, "_run_runner", runner)
    monkeypatch.setattr(controls, "diagnostics", diagnostics)
    monkeypatch.setattr(controls, "cleanup", lambda *_args: calls.append("cleanup"))
    expected = runner_code or (2 if diagnostic_error else 0)
    assert controls.run_step(step, {}) == expected
    assert calls == [("diagnostics", runner_code != 0)] + ([] if expected else ["cleanup"])
    result = controls.read_json(step.attempt / "result.json")
    assert result["complete"] is (expected == 0)
    if expected:
        assert result["resources_preserved_for_review"] is True
    assert (step.attempt / "checksums.sha256").is_file()


def test_cleanup_requires_matching_project_and_source(isolated, monkeypatch):
    step = controls.STEPS[0]
    evidence = isolated / "preparation"
    controls.write_report(
        evidence / "owned.json", {"project": "unrelated", "source": str(step.source)}
    )
    monkeypatch.setattr(
        controls, "_run", lambda *_args, **_kwargs: pytest.fail("cleanup forbidden")
    )
    with pytest.raises(controls.ControlError, match="ownership differs"):
        controls.cleanup(step, evidence)


@pytest.mark.parametrize("mode", ["--plan-only", "--prepare-only"])
def test_nonexecution_modes_never_enter_load(isolated, monkeypatch, mode):
    monkeypatch.setattr(sys, "argv", ["paired_controls", mode])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"executable":"pwsh","version":"7"}'))
    calls = []
    monkeypatch.setattr(controls, "prepare_package", lambda _launcher: calls.append("prepare") or 0)
    monkeypatch.setattr(controls, "execute_block", lambda *_args: pytest.fail("load forbidden"))
    assert controls.main() == 0
    assert calls == (["prepare"] if mode == "--prepare-only" else [])
    assert not list(isolated.iterdir())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher regression")
@pytest.mark.parametrize(
    "flag,mode",
    [("-PlanOnly", "--plan-only"), ("-PrepareOnly", "--prepare-only"), (None, "--execute")],
)
def test_real_powershell_from_other_directory_preserves_mode_output_and_exit(tmp_path, flag, mode):
    pwsh = Path(os.environ.get("CONTROL_TEST_PWSH", shutil.which("pwsh") or "missing-pwsh"))
    if not pwsh.is_file():
        pytest.skip("PowerShell 7 executable unavailable; set CONTROL_TEST_PWSH to its full path")
    root = tmp_path / "repository with spaces"
    (root / "scripts").mkdir(parents=True)
    launcher = root / "scripts/Invoke-PairedControls.ps1"
    shutil.copyfile(controls.ROOT / "scripts/Invoke-PairedControls.ps1", launcher)
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (root / "benchmarks").mkdir()
    (root / "benchmarks/__init__.py").write_text("")
    (root / "benchmarks/paired_controls.py").write_text(
        "import json,sys\nfrom pathlib import Path\nidentity=json.load(sys.stdin)\n"
        "assert Path(identity['executable']).is_file()\n"
        f"assert sys.argv[1:] == [{mode!r}]\n"
        "print('visible child progress', flush=True)\n"
        "print('specific child failure', file=sys.stderr)\nsys.exit(2)\n"
    )
    result = subprocess.run(
        [str(pwsh), "-NoProfile", "-File", str(launcher), *([flag] if flag else [])],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=""),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "visible child progress" in result.stdout
    assert "specific child failure" in result.stderr
    assert "exit code 2; no automatic retry" in result.stderr

"""No-load tests for the two fixed v1.0 controls on the current Windows build."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from benchmarks import controls_v10 as controls


def baseline() -> dict[str, object]:
    return json.loads(
        (controls.ROOT / "benchmarks/campaigns/v1-baseline-mixed.json").read_text(encoding="utf-8")
    )


def test_published_manifest_is_read_by_immutable_blob_not_assumed_in_measured_tree():
    # The tag added evidence after the measured commit; its manifest is a separate input.
    assert controls._baseline_document(controls.ROOT) == baseline()


def test_candidate_changes_only_the_preapproved_control_fields():
    published = baseline()
    candidate = controls.candidate_document(published, 1)

    assert candidate["name"] == controls.ATTEMPT_NAMES[0]
    assert candidate["official"] is False
    assert candidate["repetitions"] == 1
    assert candidate["loads"] == [published["loads"][0]]
    assert candidate["host"]["identity"]["os_build"] == controls.WINDOWS_BUILD
    assert candidate["cohorts"]["dataset_manifest"] == "../../datasets/benchmark-v1.0.json"

    for key in published.keys() - {"name", "official", "repetitions", "loads", "host", "cohorts"}:
        assert candidate[key] == published[key]
    expected_host = copy.deepcopy(published["host"])
    expected_host["identity"]["os_build"] = controls.WINDOWS_BUILD
    assert candidate["host"] == expected_host
    expected_cohorts = copy.deepcopy(published["cohorts"])
    expected_cohorts["dataset_manifest"] = "../../datasets/benchmark-v1.0.json"
    assert candidate["cohorts"] == expected_cohorts


@pytest.mark.parametrize(
    "field,value", [("official", True), ("repetitions", 2), ("profile", "timeline")]
)
def test_candidate_verifier_refuses_scope_drift(field: str, value: object):
    published = baseline()
    candidate = controls.candidate_document(published, 1)
    candidate[field] = value

    with pytest.raises(controls.ControlError):
        controls._verify_candidate(published, candidate, 1)


def test_plan_does_not_claim_readiness_or_consume_destinations(tmp_path, monkeypatch):
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "_git", lambda *_args: "")
    checkout = tmp_path / controls.WORKTREE_NAME
    attempts = tuple(tmp_path / name for name in controls.ATTEMPT_NAMES)
    plan = controls._plan(checkout, attempts, {})
    assert plan["load_executed"] is False
    assert plan["executable_preflight"] is False
    assert list(tmp_path.iterdir()) == []
    attempts[0].mkdir()
    with pytest.raises(controls.ControlError, match="no automatic retry"):
        controls._plan(checkout, attempts, {})


@pytest.mark.parametrize("existing", [controls.WORKTREE_NAME, controls.BOOTSTRAP_NAME])
def test_preparation_refuses_existing_source_or_evidence(tmp_path, monkeypatch, existing):
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    (tmp_path / existing).mkdir()
    with pytest.raises(controls.ControlError, match="no automatic retry"):
        controls.prepare_controls(
            tmp_path / controls.WORKTREE_NAME,
            tuple(tmp_path / name for name in controls.ATTEMPT_NAMES),
            {},
        )


def test_materialize_records_isolated_sync_outside_source(tmp_path, monkeypatch):
    checkout = tmp_path / controls.WORKTREE_NAME
    calls = []
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "_run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    monkeypatch.setattr(controls, "_verify_source", lambda _source: None)
    monkeypatch.setattr(
        controls, "_sync_environment", lambda source: {"python": str(source / ".venv")}
    )
    assert controls._materialize_checkout(checkout, {}) == checkout
    bootstrap = tmp_path / controls.BOOTSTRAP_NAME
    assert json.loads((bootstrap / "runtime.json").read_text())["runtime"] == {
        "python": str(checkout / ".venv")
    }
    assert not (bootstrap / "ready.json").exists()
    assert calls[0][0] == [
        "git",
        "worktree",
        "add",
        "--detach",
        str(checkout),
        controls.V10_REVISION,
    ]
    assert calls[0][1]["evidence"] == bootstrap / "worktree-add.txt"


def test_materialize_preserves_sync_failure_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controls, "_verify_source", lambda _source: None)
    calls = []

    def fail(source):
        calls.append(source)
        raise controls.ControlError("offline dependency missing")

    monkeypatch.setattr(controls, "_sync_environment", fail)
    with pytest.raises(controls.ControlError, match="environment-sync"):
        controls._materialize_checkout(tmp_path / controls.WORKTREE_NAME, {})
    assert len(calls) == 1
    report = json.loads((tmp_path / controls.BOOTSTRAP_NAME / "error.json").read_text())
    assert report["stage"] == "environment-sync"
    assert report["error"]["errors"][0]["message"] == "offline dependency missing"


@pytest.mark.parametrize("check", [False, True])
def test_sync_uses_frozen_local_cache_and_cannot_target_active_venv(tmp_path, monkeypatch, check):
    calls = []
    source = tmp_path / "source"
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "unrelated-venv")
    monkeypatch.setattr(controls.shutil, "which", lambda _name: "uv.exe")
    monkeypatch.setattr(
        controls, "_source_python", lambda _source: source / ".venv/Scripts/python.exe"
    )
    monkeypatch.setattr(controls, "_run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    controls._sync_environment(source, check=check)
    argv, kwargs = calls[0]
    assert argv[:6] == [
        "uv.exe",
        "sync",
        "--frozen",
        "--all-groups",
        "--no-python-downloads",
        "--cache-dir",
    ]
    assert argv[6] == str(controls.ROOT / ".uv-cache")
    assert ("--check" in argv) is check
    assert ("--offline" in argv) is check
    assert kwargs["cwd"] == source
    assert kwargs["environment"]["UV_PROJECT_ENVIRONMENT"] == str(source / ".venv")
    assert kwargs["environment"]["PYTHONPATH"].split(os.pathsep) == [
        str(source),
        str(source / "src"),
    ]


def test_missing_isolated_python_never_falls_back_to_active_environment(tmp_path):
    with pytest.raises(controls.ControlError, match=r"isolated v1\.0 virtual environment"):
        controls._source_python(tmp_path)


def test_real_git_checkout_accepts_forward_slashes_and_requires_detached_head(
    tmp_path, monkeypatch
):
    source = tmp_path / "frozen source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=source, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "--quiet")
    git(
        "-c",
        "user.name=Control Test",
        "-c",
        "user.email=control@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    monkeypatch.setattr(controls, "V10_REVISION", git("rev-parse", "HEAD"))
    with pytest.raises(controls.ControlError, match="detached"):
        controls._verify_source(source)
    git("checkout", "--detach", "--quiet")
    assert "\\" not in git("rev-parse", "--show-toplevel")
    controls._verify_source(source)
    candidate_dir = source / "benchmarks/results/v10-controls-win9445-candidates"
    candidate_dir.mkdir(parents=True)
    for name in controls.ATTEMPT_NAMES:
        (candidate_dir / f"{name}.json").write_text("{}")
    controls._verify_source(source, allow_candidates=True)
    (source / "unexpected.txt").write_text("preserve")
    with pytest.raises(controls.ControlError, match="outside"):
        controls._verify_source(source, allow_candidates=True)


def test_host_preflight_leaves_dynamic_admission_to_frozen_runner(tmp_path, monkeypatch):
    calls = []

    def run(_source, _environment, code, *_args, **_kwargs):
        calls.append(code)
        return CompletedProcess([], 0, '{"identity": {}}', "")

    monkeypatch.setattr(controls, "_run_source_python", run)
    controls._source_host_preflight(
        tmp_path, tmp_path / "candidate.json", {}, tmp_path / "host.txt"
    )
    assert "probe.identity()" in calls[0]
    assert "probe.dynamic(" not in calls[0]


def test_prepare_only_validates_both_candidates_without_entering_load(tmp_path, monkeypatch):
    source = tmp_path / controls.WORKTREE_NAME
    attempts = tuple(tmp_path / name for name in controls.ATTEMPT_NAMES)
    candidates = [tmp_path / f"{name}.json" for name in controls.ATTEMPT_NAMES]
    for index, candidate in enumerate(candidates, 1):
        candidate.write_text(json.dumps(controls.candidate_document(baseline(), index)))
    calls = []
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "_git", lambda *_args: "")
    monkeypatch.setattr(controls, "_materialize_checkout", lambda *_args: source)
    monkeypatch.setattr(controls, "_write_candidates", lambda *_args: candidates)
    monkeypatch.setattr(controls, "_verify_source", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controls,
        "_frozen_validation",
        lambda _source, candidate, _evidence: calls.append(candidate),
    )
    monkeypatch.setattr(controls, "_assert_project_absent", lambda *_args: None)
    monkeypatch.setattr(controls, "_image_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "_source_host_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "_control_fingerprints", lambda *_args: {"driver": "verified"})
    monkeypatch.setattr(controls, "_run", lambda argv, **_kwargs: calls.append(argv))
    monkeypatch.setattr(controls, "_run_attempt", lambda *_args: pytest.fail("load is forbidden"))
    assert controls.execute(source, attempts, {}, plan_only=False, prepare_only=True) == 0
    assert calls[:2] == candidates
    assert calls[2][-4:] == ["--profile", "campaign", "config", "--quiet"]
    report = json.loads((tmp_path / controls.BOOTSTRAP_NAME / "ready.json").read_text())
    assert report["load_executed"] is False
    assert all(not attempt.exists() for attempt in attempts)


def test_execute_requires_actual_preparation_and_stops_after_first_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    monkeypatch.setattr(controls, "_plan", lambda *_args: {})
    source = tmp_path / controls.WORKTREE_NAME
    attempts = tuple(tmp_path / name for name in controls.ATTEMPT_NAMES)
    with pytest.raises(controls.ControlError, match="verified preparation required"):
        controls.execute(source, attempts, {}, plan_only=False)
    calls = []
    monkeypatch.setattr(
        controls, "_prepared_candidates", lambda *_args: [tmp_path / "a.json", tmp_path / "b.json"]
    )
    monkeypatch.setattr(
        controls,
        "_run_attempt",
        lambda _source, _candidate, attempt, _launcher: calls.append(attempt) or 2,
    )
    assert controls.execute(source, attempts, {}, plan_only=False) == 2
    assert calls == [attempts[0]]


def test_external_command_failure_keeps_diagnostic_and_specific_exit_code(tmp_path):
    evidence = tmp_path / "failure.txt"
    with pytest.raises(controls.ControlError, match="exited 7; diagnostics:"):
        controls._run(
            [
                sys.executable,
                "-c",
                "import sys; print('expected failure', file=sys.stderr); sys.exit(7)",
            ],
            cwd=tmp_path,
            evidence=evidence,
        )
    assert evidence.read_text() == "expected failure"


@pytest.mark.parametrize("owned", [False, True])
def test_failed_runner_preserves_evidence_and_never_cleans_up(tmp_path, monkeypatch, owned):
    monkeypatch.setattr(controls, "RESULTS", tmp_path)
    source = tmp_path / "source"
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(controls.candidate_document(baseline(), 1)))
    monkeypatch.setattr(controls, "_baseline_document", lambda _source: baseline())
    monkeypatch.setattr(controls, "_verify_source", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controls, "_assert_project_absent", lambda *_args: None)
    monkeypatch.setattr(controls, "_image_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "_source_host_preflight", lambda *_args: {})
    monkeypatch.setattr(controls, "_source_python", lambda *_args: Path(sys.executable))
    calls = []
    attempt = tmp_path / controls.ATTEMPT_NAMES[0]

    def failed_runner(argv, **_kwargs):
        calls.append(argv)
        if owned:
            (attempt / "preparation").mkdir()
            (attempt / "preparation/owned.json").write_text("{}")
        return CompletedProcess(argv, 2, "", "")

    monkeypatch.setattr(controls, "_run_runner", failed_runner)
    monkeypatch.setattr(
        controls,
        "_run",
        lambda argv, **_kwargs: calls.append(argv) or CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(controls, "_capture_diagnostics", lambda _source, _env, dest: dest.mkdir())
    assert controls._run_attempt(source, candidate, attempt, {}) == 2
    assert len(calls) == (2 if owned else 1)
    assert "benchmarks.run_campaign" in calls[0]
    assert all("down" not in argv for argv in calls)
    if owned:
        assert calls[1][-4:] == ["stop", "--timeout", "10", "loadgen"]
    report = json.loads((attempt / "result.json").read_text())
    assert report["complete"] is False
    assert report["resources_preserved_for_review"] is True
    assert (attempt / "checksums.sha256").is_file()


@pytest.mark.parametrize("grace_timeout", [False, True])
def test_runner_interrupt_waits_for_frozen_cleanup_without_retry(monkeypatch, grace_timeout):
    calls = []

    class Process:
        args = ("frozen-runner",)
        returncode = 0

        def communicate(self, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise KeyboardInterrupt
            if timeout == 90 and grace_timeout:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return "preserved progress", "interrupted"

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(controls.signal, "signal", lambda *_args: None)
    result = controls._wait_for_runner(Process())
    assert result.returncode == 130
    assert result.stdout == "preserved progress"
    assert calls == ([None, 90, "kill", 10] if grace_timeout else [None, 90])


def test_stdin_distinguishes_plan_preparation_and_load(monkeypatch):
    options = {
        "checkout": str(controls.RESULTS / controls.WORKTREE_NAME),
        "attempts": [str(controls.RESULTS / name) for name in controls.ATTEMPT_NAMES],
        "launcher": {},
        "plan_only": False,
        "prepare_only": True,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(options)))
    assert controls._options_from_stdin()[-2:] == (False, True)
    options["plan_only"] = True
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(options)))
    with pytest.raises(controls.ControlError, match="options are invalid"):
        controls._options_from_stdin()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher regression")
def test_real_powershell_launcher_shows_child_error_and_preserves_exit_code(tmp_path):
    pwsh = Path(os.environ.get("CONTROL_TEST_PWSH", shutil.which("pwsh") or "missing-pwsh"))
    if not pwsh.is_file():
        pytest.skip("PowerShell 7 executable unavailable; set CONTROL_TEST_PWSH to its full path")
    root = tmp_path / "repository with spaces"
    (root / "scripts").mkdir(parents=True)
    launcher = root / "scripts/Invoke-V10Controls.ps1"
    shutil.copyfile(controls.ROOT / "scripts/Invoke-V10Controls.ps1", launcher)
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (root / "benchmarks").mkdir()
    (root / "benchmarks/__init__.py").write_text("")
    (root / "benchmarks/controls_v10.py").write_text(
        "import json,sys\noptions=json.load(sys.stdin)\n"
        "assert options['prepare_only'] is True and options['plan_only'] is False\n"
        "print('visible child progress', flush=True)\n"
        "print('specific child failure', file=sys.stderr)\nsys.exit(2)\n"
    )
    environment = dict(os.environ, PYTHONPATH="")
    result = subprocess.run(
        [str(pwsh), "-NoProfile", "-File", str(launcher), "-PrepareOnly"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "visible child progress" in result.stdout
    assert "specific child failure" in result.stderr
    assert "stopped with exit code 2" in result.stderr

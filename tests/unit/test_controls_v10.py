"""No-load tests for the two fixed v1.0 controls on the current Windows build."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from benchmarks import controls_v10 as controls


def baseline() -> dict[str, object]:
    return json.loads(
        (controls.ROOT / "benchmarks/campaigns/v1-baseline-mixed.json").read_text(encoding="utf-8")
    )


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


def test_plan_only_rejects_existing_destinations_without_creating_anything(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(controls, "RESULTS", results)
    monkeypatch.setattr(controls, "_git", lambda *_args: "")
    checkout = results / controls.WORKTREE_NAME
    attempts = tuple(results / name for name in controls.ATTEMPT_NAMES)

    plan = controls._plan(checkout, attempts, {"executable": "pwsh.exe"})

    assert plan["load_executed"] is False
    assert not any(path.exists() for path in (checkout, *attempts))
    checkout.mkdir()
    with pytest.raises(controls.ControlError):
        controls._plan(checkout, attempts, {"executable": "pwsh.exe"})


def test_plan_only_rejects_an_existing_bootstrap_destination(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(controls, "RESULTS", results)
    monkeypatch.setattr(controls, "_git", lambda *_args: "")
    (results / controls.BOOTSTRAP_NAME).mkdir()

    with pytest.raises(controls.ControlError):
        controls._plan(
            results / controls.WORKTREE_NAME,
            tuple(results / name for name in controls.ATTEMPT_NAMES),
            {"executable": "pwsh.exe"},
        )


def test_materialize_records_verified_read_only_runtime_outside_source(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    checkout = results / controls.WORKTREE_NAME
    calls: list[tuple[list[str], Path, Path | None]] = []

    def run(argv, *, cwd, evidence=None, **_kwargs):
        calls.append((list(argv), cwd, evidence))
        return CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(controls, "RESULTS", results)
    monkeypatch.setattr(controls, "_run", run)
    monkeypatch.setattr(controls, "_verify_source", lambda _source: None)
    monkeypatch.setattr(
        controls,
        "_runtime_parity",
        lambda _source: {"mode": "verified-read-only-root-venv"},
    )

    assert controls._materialize_checkout(checkout, {"executable": "pwsh.exe"}) == checkout

    bootstrap = results / controls.BOOTSTRAP_NAME
    assert (bootstrap / "launcher.json").is_file()
    assert (bootstrap / "ready.json").is_file()
    assert calls == [
        (
            ["git", "worktree", "add", "--detach", str(checkout), controls.V10_REVISION],
            controls.ROOT,
            bootstrap / "worktree-add.txt",
        )
    ]
    assert json.loads((bootstrap / "runtime-parity.json").read_text()) == {
        "mode": "verified-read-only-root-venv"
    }
    assert all(evidence is None or evidence.parent == bootstrap for _, _, evidence in calls)
    assert all(
        evidence is None or not evidence.is_relative_to(checkout) for _, _, evidence in calls
    )


def test_materialize_preserves_a_stage_specific_bootstrap_error(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    checkout = results / controls.WORKTREE_NAME

    def run(argv, **_kwargs):
        return CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(controls, "RESULTS", results)
    monkeypatch.setattr(controls, "_run", run)
    monkeypatch.setattr(controls, "_verify_source", lambda _source: None)
    monkeypatch.setattr(
        controls,
        "_runtime_parity",
        lambda _source: (_ for _ in ()).throw(controls.ControlError("runtime differs")),
    )

    with pytest.raises(controls.ControlError, match="runtime-parity"):
        controls._materialize_checkout(checkout, {"executable": "pwsh.exe"})

    report = json.loads((results / controls.BOOTSTRAP_NAME / "error.json").read_text())
    assert report["stage"] == "runtime-parity"
    assert report["error"]["errors"][0]["message"] == "runtime differs"


def test_runtime_parity_requires_identical_third_party_lock_and_frozen_source(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "source"
    root.mkdir()
    source.mkdir()
    (root / "uv.lock").write_text("", encoding="utf-8")
    (source / "uv.lock").write_text("", encoding="utf-8")
    frozen_fulfillflow = source / "src" / "fulfillflow" / "__init__.py"
    frozen_benchmarks = source / "benchmarks" / "__init__.py"
    frozen_fulfillflow.parent.mkdir(parents=True)
    frozen_benchmarks.parent.mkdir(parents=True)
    frozen_fulfillflow.write_text("", encoding="utf-8")
    frozen_benchmarks.write_text("", encoding="utf-8")
    frozen = {
        "fulfillflow": {"name": "fulfillflow", "version": "1.0.0"},
        "psycopg": {"name": "psycopg", "version": "3.3.4"},
        "uvloop": {"name": "uvloop", "version": "0.22.1"},
    }
    active = {
        "fulfillflow": {"name": "fulfillflow", "version": "1.1.0"},
        "psycopg": {"name": "psycopg", "version": "3.3.4"},
        "uvloop": {"name": "uvloop", "version": "0.22.1"},
    }

    monkeypatch.setattr(controls, "ROOT", root)
    monkeypatch.setattr(
        controls,
        "_lock_packages",
        lambda path: frozen.copy() if path == source / "uv.lock" else active.copy(),
    )
    monkeypatch.setattr(
        controls,
        "_run_source_python",
        lambda *_args, **_kwargs: CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "platform": "win32",
                    "python": "C:/runtime/python.exe",
                    "packages": {"psycopg": "3.3.4"},
                    "code": {
                        "fulfillflow": str(frozen_fulfillflow),
                        "benchmarks": str(frozen_benchmarks),
                    },
                }
            ),
            "",
        ),
    )

    report = controls._runtime_parity(source)

    assert report["mode"] == "verified-read-only-root-venv"
    assert report["windows_lock_exclusions"] == ["uvloop"]


def test_runtime_parity_refuses_dependency_lock_drift(tmp_path, monkeypatch):
    root = tmp_path / "root"
    source = tmp_path / "source"
    root.mkdir()
    source.mkdir()
    frozen = {
        "fulfillflow": {"name": "fulfillflow", "version": "1.0.0"},
        "psycopg": {"name": "psycopg", "version": "3.3.4"},
    }
    drifted = {
        "fulfillflow": {"name": "fulfillflow", "version": "1.1.0"},
        "psycopg": {"name": "psycopg", "version": "3.3.5"},
    }

    monkeypatch.setattr(controls, "ROOT", root)
    monkeypatch.setattr(
        controls,
        "_lock_packages",
        lambda path: frozen.copy() if path == source / "uv.lock" else drifted.copy(),
    )

    with pytest.raises(controls.ControlError, match="active dependency lock differs"):
        controls._runtime_parity(source)


def test_execution_stops_after_the_first_failed_control(tmp_path, monkeypatch):
    checkout = tmp_path / controls.WORKTREE_NAME
    attempts = tuple(tmp_path / name for name in controls.ATTEMPT_NAMES)
    source = tmp_path / "frozen-source"
    candidates = [tmp_path / "candidate-01.json", tmp_path / "candidate-02.json"]
    calls: list[Path] = []
    monkeypatch.setattr(controls, "_plan", lambda *_args: {})
    monkeypatch.setattr(controls, "_materialize_checkout", lambda *_args: source)
    monkeypatch.setattr(controls, "_write_candidates", lambda *_args: candidates)
    monkeypatch.setattr(
        controls,
        "_run_attempt",
        lambda _source, _candidate, attempt, _launcher: calls.append(attempt) or 2,
    )

    assert controls.execute(checkout, attempts, {}, plan_only=False) == 2
    assert calls == [attempts[0]]


def test_isolated_source_allows_only_its_two_generated_candidates(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    candidate_directory = "benchmarks/results/v10-controls-win9445-candidates"
    candidates = "\n".join(
        f"?? {candidate_directory}/{name}.json" for name in controls.ATTEMPT_NAMES
    )

    def git(_root: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(source)
        if arguments == ("rev-parse", "HEAD"):
            return controls.V10_REVISION
        if arguments == ("status", "--porcelain", "--untracked-files=all"):
            return candidates
        raise AssertionError(arguments)

    monkeypatch.setattr(controls, "_git", git)
    controls._verify_source(source, allow_candidates=True)

    candidates += "\n?? benchmarks/campaigns/unapproved.json"
    with pytest.raises(controls.ControlError):
        controls._verify_source(source, allow_candidates=True)

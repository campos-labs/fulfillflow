"""No-load tests for the two fixed v1.0 controls on the current Windows build."""

from __future__ import annotations

import copy
import json
from pathlib import Path

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

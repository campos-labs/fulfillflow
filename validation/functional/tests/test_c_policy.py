"""Historical records are read-only regression fixtures, not new application evidence."""

import json
from pathlib import Path

import pytest
from validation.functional.c_policy import decision

BASE = Path(__file__).resolve().parents[1] / ".artifacts"


def fixture(tmp_path):
    source = BASE / "c-outage-v12-dev-01/v1.2.0-rc.1-unavailable"
    for name in (
        "result.json",
        "dependency-stopped.json",
        "dependency-restored.json",
        "restoration-schedule.json",
        "authenticated-readiness.json",
        "request-completion.json",
        "after-dependency-restore.json",
    ):
        (tmp_path / name).write_bytes((source / name).read_bytes())
    record = json.loads((tmp_path / "result.json").read_text())
    record["mode"] = "evaluated"
    (tmp_path / "result.json").write_text(json.dumps(record))
    return {"exit_code": 2, "timed_out": False, "tree_empty": True, "forced_tree_cleanup": False}


def test_qualified_pending_remains_inconclusive(tmp_path):
    assert decision(tmp_path, "v1.2.0-rc.1", "unavailable", fixture(tmp_path)) == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "version",
        "extra_call",
        "timeout",
        "cleanup",
        "identity",
        "sql",
        "effects",
        "schedule",
        "forced",
    ],
)
def test_incomplete_or_divergent_evidence_blocks(tmp_path, change):
    process = fixture(tmp_path)
    name = "result.json"
    data = json.loads((tmp_path / name).read_text())
    if change == "missing":
        (tmp_path / "request-completion.json").unlink()
    elif change == "version":
        data["version"] = "v1.0.0"
    elif change == "extra_call":
        data["webhook_calls"].append(data["webhook_calls"][0])
    elif change == "timeout":
        process["timed_out"] = True
    elif change == "forced":
        process["forced_tree_cleanup"] = True
    elif change == "cleanup":
        data["cleanup"] = []
    else:
        name = {
            "identity": "request-completion.json",
            "sql": "authenticated-readiness.json",
            "effects": "after-dependency-restore.json",
            "schedule": "restoration-schedule.json",
        }[change]
        data = json.loads((tmp_path / name).read_text())
        if change == "identity":
            data["core"]["pid"] = -1
        elif change == "sql":
            data["core"]["select_one"] = 0
        elif change == "effects":
            data["core"]["notifications"] = [{"id": "unexpected"}]
        elif change == "schedule":
            data["actual_seconds_from_offer"] = 40
    (tmp_path / name).write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        decision(tmp_path, "v1.2.0-rc.1", "unavailable", process)


@pytest.mark.parametrize("corrupt", [False, True])
def test_pass_requires_consistent_final_snapshot(tmp_path, corrupt):
    source = BASE / "c-common-dev-01/v1.2.0-rc.1-healthy"
    for name in ("result.json", "final.json"):
        (tmp_path / name).write_bytes((source / name).read_bytes())
    record = json.loads((tmp_path / "result.json").read_text())
    record["mode"] = "evaluated"
    (tmp_path / "result.json").write_text(json.dumps(record))
    process = {"exit_code": 0, "timed_out": False, "tree_empty": True, "forced_tree_cleanup": False}
    if corrupt:
        final = json.loads((tmp_path / "final.json").read_text())
        final["core"]["notifications"] = []
        (tmp_path / "final.json").write_text(json.dumps(final))
        with pytest.raises(ValueError, match="FINAL_INVARIANTS"):
            decision(tmp_path, "v1.2.0-rc.1", "healthy", process)
    else:
        assert decision(tmp_path, "v1.2.0-rc.1", "healthy", process) == "PASS"

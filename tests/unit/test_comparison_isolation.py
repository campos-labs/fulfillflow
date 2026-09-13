import copy
import json
import subprocess

import pytest
from benchmarks import comparison_isolation as isolation
from benchmarks.controls_v10 import ControlError


@pytest.mark.parametrize(
    "failure", [None, "identity", "export", "shutdown", "environment", "concurrency"]
)
def test_manual_isolation_preserves_and_stops_first_failure(tmp_path, monkeypatch, failure):
    rows = [
        {
            "id": str(i),
            "name": f"/prior-{role}-1",
            "image": "frozen",
            "project": "prior",
            "mounts": [],
            "running": True,
            "exit_code": 0,
            "oom": False,
        }
        for i, role in enumerate(("app", "db"))
    ]
    (tmp_path / "isolation.json").write_text(json.dumps({"containers": rows, "preserved": {}}))
    stopped = set()
    commands = []

    def inspect(identifier):
        row = copy.deepcopy(rows[int(identifier)])
        if failure == "identity":
            row["image"] = "different"
        if identifier in stopped:
            row.update(running=False, exit_code=143 if identifier == "0" else 0)
        return row

    def capture(args):
        commands.append(args)
        output = ""
        if args[0] == "ps":
            output = "0\n1\n" + ("unexpected\n" if failure == "concurrency" else "")
        if args[0] == "logs":
            if failure == "export" and not stopped:
                raise subprocess.CalledProcessError(1, ["docker", *args], stderr="export failed")
            if failure != "shutdown":
                output = "Application shutdown complete.\ndatabase system is shut down"
        if args[0] == "stop":
            stopped.add(args[-1])
        return subprocess.CompletedProcess(args, 0, output, "")

    def energy():
        if failure == "environment":
            raise ControlError("environment failed")

    monkeypatch.setattr(isolation, "capture", capture)
    monkeypatch.setattr(isolation, "inspect", inspect)
    monkeypatch.setattr(isolation, "require_energy", energy)
    destination = tmp_path / "new output"
    if failure:
        with pytest.raises((ControlError, subprocess.CalledProcessError)):
            isolation.isolate(tmp_path, destination)
    else:
        isolation.isolate(tmp_path, destination)
    assert len(stopped) == (2 if failure is None else 1 if failure == "shutdown" else 0)
    assert not any(c[0] in ("rm", "start", "restart", "compose") for c in commands)
    assert json.loads((destination / "result.json").read_text())["complete"] == (failure is None)
    before = len(commands)
    with pytest.raises(ControlError, match="no retry"):
        isolation.isolate(tmp_path, destination)
    assert len(commands) == before


def test_mount_order_is_not_identity_but_values_are(monkeypatch):
    d = {
        "Id": "id",
        "Name": "name",
        "Image": "image",
        "Config": {"Labels": {}},
        "State": {"Running": True, "ExitCode": 0, "OOMKilled": False},
        "Mounts": [{"Destination": "/z", "Source": "one"}, {"Destination": "/a", "Source": "two"}],
    }
    monkeypatch.setattr(
        isolation, "capture", lambda a: subprocess.CompletedProcess(a, 0, json.dumps([d]), "")
    )
    first = isolation.inspect("id")
    d["Mounts"].reverse()
    assert isolation.inspect("id") == first
    d["Mounts"][0]["Source"] = "changed"
    assert isolation.inspect("id") != first

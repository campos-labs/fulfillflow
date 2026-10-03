"""Coordinator failure injection without Docker, application execution or credentials."""

import json
import subprocess

import pytest
from validation.functional import c_qualify


@pytest.mark.parametrize("failure", ["case_exit", "timeout"])
def test_first_child_failure_stops_sequence_and_preserves_summary(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(c_qualify, "BASE", tmp_path)
    monkeypatch.setattr(c_qualify.secrets, "token_hex", lambda size: "test")
    monkeypatch.setattr(c_qualify, "verify_export", lambda record: None)
    runtime = tmp_path / ".artifacts/c-runtimes-01/v1.0.0"
    runtime.mkdir(parents=True)
    (runtime / "source.json").write_text("{}")
    stopped = set()
    cases = []

    def fake_run(args, **kwargs):
        if args[0] != c_qualify.DOCKER:
            cases.append(args)
            if failure == "timeout":
                raise subprocess.TimeoutExpired("redacted", 480)
            return subprocess.CompletedProcess(args, 2, b"", b"")
        cmd = args[1:]
        value = ""
        if cmd[0] == "image":
            value = json.dumps([{"RepoDigests": [cmd[-1]]}])
        elif cmd[0] == "run":
            value = cmd[cmd.index("--name") + 1]
        elif cmd[0] == "inspect":
            role = "postgres" if cmd[1].endswith("postgres") else "rabbit"
            port = "5432/tcp" if role == "postgres" else "5672/tcp"
            value = json.dumps(
                [
                    {
                        "Config": {"Labels": {"org.fulfillflow.c.owner": "ff-c-tcp-test"}},
                        "Image": "image",
                        "State": {
                            "Running": cmd[1] not in stopped,
                            "ExitCode": 0,
                            "OOMKilled": False,
                        },
                        "NetworkSettings": {"Ports": {port: [{"HostPort": "12345"}]}},
                    }
                ]
            )
        elif cmd[0] == "stop":
            stopped.add(cmd[-1])
        return subprocess.CompletedProcess(args, 0, value.encode(), b"")

    monkeypatch.setattr(c_qualify.subprocess, "run", fake_run)
    expected = RuntimeError if failure == "case_exit" else subprocess.TimeoutExpired
    with pytest.raises(expected):
        c_qualify.main(tmp_path / "result", ("healthy", "duplicate"))
    report = json.loads((tmp_path / "result/summary.json").read_text())
    assert len(cases) == 1 and len(stopped) == 2
    assert len(report["cleanup"]) == 2 and all(not x["running"] for x in report["cleanup"])
    assert report["error"]["type"] == expected.__name__


@pytest.mark.parametrize(
    "row",
    [
        {"error_type": "TimeoutError"},
        {"running": True, "exit_code": 0, "oom": False},
        {"running": False, "exit_code": 1, "oom": False},
        {"running": False, "exit_code": 0, "oom": True},
    ],
)
def test_cleanup_failures_cannot_approve(row):
    assert not c_qualify.cleanup_succeeded([row])
    assert c_qualify.cleanup_succeeded([{"running": False, "exit_code": 0, "oom": False}])


def test_unknown_version_rejected_before_output(tmp_path):
    with pytest.raises(ValueError, match="INVALID_VERSIONS"):
        c_qualify.main(tmp_path / "new", ("unavailable",), ("v1.3",))
    assert not (tmp_path / "new").exists()

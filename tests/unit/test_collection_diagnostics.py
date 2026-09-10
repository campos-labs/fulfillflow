"""Original failures survive resource supervision without sensitive rendering."""

import json
import subprocess
import threading
import time

import pytest
from benchmarks import collectors, resource_snapshot, run_campaign
from benchmarks.collection_diagnostics import PREFIX, counters, failure


@pytest.mark.parametrize("stage", ["endpoint", "connections", "snapshot", "delta", "write"])
def test_failure_retains_class_stage_and_no_exception_text(stage):
    secret = "password=secret-value https://private.invalid/credentials"
    inner = subprocess.CalledProcessError(7, [secret], stderr=secret)
    outer = RuntimeError(secret)
    outer.__cause__ = inner
    report = failure(outer, stage, time.monotonic())
    assert report["stage"] == stage
    assert report["errors"] == [
        {"type": "RuntimeError"},
        {"type": "CalledProcessError", "returncode": 7},
    ]
    assert secret not in json.dumps(report)


def test_helper_original_exception_is_retained_without_forwarding_untrusted_fields():
    payload = {
        "stage": "transport",
        "errors": [{"type": "OSError", "secret": "private"}],
        "raw_body": "private",
    }
    error = subprocess.CalledProcessError(2, ["private"], stderr=PREFIX + json.dumps(payload))
    report = failure(error, "snapshot", time.monotonic())
    assert report["errors"][0]["helper_exception_type"] == "OSError"
    assert report["errors"][0]["helper_stage"] == "transport"
    assert "private" not in json.dumps(report)


def test_counter_projection_ignores_arbitrary_fields():
    assert counters({"cpu": 1, "read": "secret", "raw_body": "secret", "cpus": True}) == {"cpu": 1}


@pytest.mark.parametrize("problem", ["snapshot", "connections", "delta", "output"])
def test_sampler_retains_original_failure_and_notifies_supervisor(tmp_path, monkeypatch, problem):
    class Database:
        def connection_counts(self):
            if problem == "connections":
                raise ValueError("private database address")
            return {"postgres": 1}

    def command(argv, _timeout):
        return subprocess.CompletedProcess(argv, 0, "unix:///safe", "")

    sampler = collectors.ResourceSampler(
        tmp_path / "resources.csv", {"postgres": "p"}, Database(), 1, command_runner=command
    )
    sample = {
        "cpu": 1,
        "system": 1,
        "cpus": 1,
        "memory": 1,
        "limit": 1,
        "read": "2026-09-10T00:00:00Z",
    }

    def snapshot(_endpoint):
        if problem == "snapshot":
            raise OSError("private socket address")
        return {"p": dict(sample)}

    monkeypatch.setattr(sampler, "_snapshot", snapshot)
    if problem == "output":
        sampler.output_path = tmp_path / "not-directory" / "resources.csv"
        sampler.output_path.parent.write_text("occupied")
    sampler.start()
    assert sampler.failed.wait(2)
    with pytest.raises(collectors.ExternalCommandError) as caught:
        sampler.stop()
    assert caught.value.__cause__ is sampler._exception
    assert sampler.failure["stage"] == problem
    assert "private" not in json.dumps(sampler.failure)
    if problem == "delta":
        assert sampler.failure["previous"] == sampler.failure["current"] == sample
    if problem == "output":
        assert sampler.failure["diagnostic_write_error"]["stage"] == "output"
    else:
        assert (
            json.loads(sampler.output_path.with_suffix(".error.json").read_text())
            == sampler.failure
        )


def test_supervisor_checks_failure_between_bounded_waits(monkeypatch):
    class Sampler:
        failed = threading.Event()

    waits = []

    class Process:
        def wait(self, timeout):
            waits.append(timeout)
            Sampler.failed.set()
            raise subprocess.TimeoutExpired("private", timeout)

    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: Process())
    managed = run_campaign.ManagedProcess(["unused"])
    with pytest.raises(collectors.ExternalCommandError, match="mandatory"):
        managed.wait(300, sampler=Sampler())
    assert waits == [0.25]


def test_helper_diagnostic_does_not_change_successful_output(monkeypatch, capsys):
    monkeypatch.setattr(resource_snapshot.sys, "argv", ["snapshot", "unix:///safe", "a" * 64])
    monkeypatch.setattr(
        resource_snapshot,
        "read_stats",
        lambda *_: {
            "read": "2026-09-10T00:00:00Z",
            "cpu_stats": {
                "cpu_usage": {"total_usage": 11},
                "system_cpu_usage": 20,
                "online_cpus": 2,
            },
            "memory_stats": {"usage": 100, "limit": 200, "stats": {"inactive_file": 40}},
        },
    )
    assert resource_snapshot.main() == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["a" * 64] == {
        "read": "2026-09-10T00:00:00Z",
        "cpu": 11,
        "system": 20,
        "cpus": 2,
        "memory": 60,
        "limit": 200,
    }


def test_runner_provenance_rejects_dirty_source(tmp_path, monkeypatch):
    monkeypatch.setattr(
        run_campaign,
        "_git_provenance",
        lambda _: run_campaign.GitProvenance("a" * 40, "codex/test", False, True),
    )
    with pytest.raises(run_campaign.CampaignExecutionError, match="clean"):
        run_campaign.runner_provenance(tmp_path)


def test_runner_provenance_identifies_code_and_lock(tmp_path, monkeypatch):
    (tmp_path / "benchmarks").mkdir()
    source = tmp_path / "benchmarks/collectors.py"
    source.write_text("first")
    (tmp_path / "uv.lock").write_text("locked")
    monkeypatch.setattr(
        run_campaign,
        "_git_provenance",
        lambda _: run_campaign.GitProvenance("a" * 40, "codex/test", True, True),
    )
    first = run_campaign.runner_provenance(tmp_path)
    source.write_text("second")
    second = run_campaign.runner_provenance(tmp_path)
    assert first["components"] != second["components"]
    assert first["supervision_interval_seconds"] == 0.25

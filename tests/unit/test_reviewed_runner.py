"""Independent application provenance and simultaneous failures remain fail closed."""

import json
import subprocess
from pathlib import Path

import pytest
from benchmarks import run_campaign as runner
from benchmarks.campaign import load_campaign
from benchmarks.collectors import ExternalCommandError, ObservedEnvironment


@pytest.mark.parametrize("defect", ["root", "sha", "release", "application-dirty", "runner-dirty"])
def test_separate_source_refuses_provenance_drift_before_preparation(tmp_path, monkeypatch, defect):
    source = tmp_path / "application"
    source.mkdir()
    bundle = load_campaign(Path("benchmarks/fixtures/smoke-campaign.json"))
    monkeypatch.setattr(
        runner,
        "run_capture",
        lambda *_: subprocess.CompletedProcess(
            [], 0, str(tmp_path if defect == "root" else source), ""
        ),
    )
    monkeypatch.setattr(
        runner,
        "_project_release",
        lambda _: "v1.1.0" if defect == "release" else bundle.manifest.release,
    )
    monkeypatch.setattr(
        runner,
        "_git_provenance",
        lambda root: runner.GitProvenance(
            "0" * 40 if defect == "sha" else bundle.manifest.git_sha,
            "",
            not (
                (root == source and defect == "application-dirty")
                or (root != source and defect == "runner-dirty")
            ),
            True,
        ),
    )
    monkeypatch.setattr(runner, "_run_preparation", lambda *_: pytest.fail("preparation forbidden"))
    with pytest.raises(runner.CampaignExecutionError):
        runner._execute(
            bundle,
            Path("unused"),
            "http://unused",
            tmp_path / "result",
            ["unused"],
            application_source=source,
        )
    assert not (tmp_path / "result").exists()


def test_phase_retains_process_error_and_collector_export_secondary_failures(tmp_path, monkeypatch):
    bundle = load_campaign(Path("benchmarks/fixtures/smoke-campaign.json"))
    original = runner.CampaignExecutionError("external process exceeded its frozen timeout")

    class Process:
        def wait(self, *_args, **_kwargs):
            raise original

        def ensure_stopped(self):
            raise OSError("private process argument")

    class Sampler:
        def __init__(self):
            self.failure = {"stage": "connections", "errors": [{"type": "ValueError"}]}

        def start(self):
            pass

        def stop(self):
            raise ExternalCommandError("private DSN")

    monkeypatch.setattr(runner, "ManagedProcess", lambda *_: Process())
    monkeypatch.setattr(runner, "ResourceSampler", lambda *_a, **_k: Sampler())
    monkeypatch.setattr(runner, "_wait_for_container_file", lambda *_: None)
    monkeypatch.setattr(runner, "_terminate_container_phase", lambda *_: None)

    def export(*_):
        raise OSError("private export path")

    monkeypatch.setattr(runner, "run_capture", export)
    observed = ObservedEnvironment({}, {"loadgen": "id"}, "user", "database")
    with pytest.raises(runner.CampaignExecutionError) as caught:
        runner._run_phase(
            bundle,
            bundle.manifest.loads[0],
            "measurement",
            "http://unused",
            observed,
            object(),
            "/runtime",
            tmp_path,
        )
    assert caught.value is original
    report = json.loads((tmp_path / "phase-error.json").read_text())
    assert report["errors"][0]["type"] == "CampaignExecutionError"
    assert len(report["secondary_errors"]) == 2
    assert report["collector"]["stage"] == "connections"
    assert report["export_error"]["errors"][0]["type"] == "OSError"
    assert "private" not in json.dumps(report)

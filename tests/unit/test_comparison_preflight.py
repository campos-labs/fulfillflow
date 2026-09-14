import subprocess

import pytest
from benchmarks import active_comparison_controls as active
from benchmarks import controls_v10 as controls


def test_external_error_keeps_stage_executable_code_and_sanitized_stderr(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "private-value")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, "", "engine unavailable token=private-value https://secret.invalid"
        ),
    )
    with pytest.raises(controls.ExternalCommandError) as exc:
        controls._run(["docker", "info", "sensitive-argument"], cwd=tmp_path, stage="engine")
    d = exc.value.command_diagnostic
    assert (d["stage"], d["executable"], d["exit_code"]) == ("engine", "docker", 1)
    assert "engine unavailable" in d["stderr"]
    assert "private-value" not in str(exc.value)
    assert "sensitive-argument" not in str(exc.value)


def test_unavailable_engine_precedes_destination_checks(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["stage"])
        if argv[1] == "info":
            raise controls.ExternalCommandError(
                {"stage": "docker_engine_availability", "exit_code": 1}
            )
        return subprocess.CompletedProcess(argv, 0, "desktop-linux", "")

    monkeypatch.setattr(active, "_run", run)
    monkeypatch.setattr(
        active.comparison, "verify_draft", lambda: pytest.fail("must not reach package")
    )
    with pytest.raises(controls.ExternalCommandError):
        active.preflight()
    assert calls == ["docker_context", "docker_engine_availability"]

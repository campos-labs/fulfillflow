"""Native process identity and no-side-effect rejection for C development supervision."""

import os
from pathlib import Path

import pytest
from validation.functional.c_tcp_case import environment, launch, scenario


def test_environment_does_not_inherit_database_or_python_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "historical-resource")
    monkeypatch.setenv("PYTHONPATH", "wrong-source")
    env = environment(tmp_path / "source", tmp_path, {})
    assert "DATABASE_URL" not in env
    assert "wrong-source" not in env["PYTHONPATH"]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_native_process_pid_is_owned(tmp_path: Path):
    child = launch(
        ["-c", "import os,sys;print(os.getpid());print(sys.prefix)"], tmp_path, tmp_path, {}
    )
    try:
        output, _ = child.communicate(timeout=10)
        assert child.returncode == 0
        lines = output.decode().splitlines()
        assert int(lines[0]) == child.pid
        assert Path(lines[1]).resolve() == Path(os.sys.prefix).resolve()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
async def test_invalid_action_does_not_create_output(tmp_path):
    output = tmp_path / "must-not-exist"
    with pytest.raises(AssertionError, match="INVALID_ACTION"):
        await scenario(output, tmp_path / "missing.json", "retry")
    assert not output.exists()


@pytest.mark.asyncio
async def test_output_outside_scope_is_rejected(tmp_path, monkeypatch):
    from validation.functional import c_tcp_case

    monkeypatch.setattr(c_tcp_case, "FOLDER", tmp_path / "allowed")
    output = tmp_path / "must-not-exist"
    with pytest.raises(AssertionError, match="OUTPUT_OUTSIDE_SCOPE"):
        await scenario(output, tmp_path / "missing.json", "control")
    assert not output.exists()


@pytest.mark.parametrize(
    "tag,kind",
    [
        ("v1.0.0", "sql_before_commit"),
        ("v1.1.0-rc.1", "sql_before_commit"),
        ("v1.2.0-rc.1", "handler_sql_before_done_and_commit"),
    ],
)
def test_marker_rejects_error_or_wrong_identity(tag, kind):
    from validation.functional.c_tcp_case import valid_marker

    marker = dict(marker=kind, pid=42, token="t", event_id="e")
    assert valid_marker(marker, tag, 42, "t", "e")
    for key, value in (("marker", "error"), ("pid", 43), ("token", "other"), ("event_id", "other")):
        assert not valid_marker(dict(marker, **{key: value}), tag, 42, "t", "e")


def test_technical_completion_requires_durable_rows():
    from validation.functional.c_tcp_case import technical_complete

    data = {
        owner: {"message_inbox": [{"state": "DONE"}], "message_outbox": [{"state": "SENT"}]}
        for owner in ("core", "tracking")
    }
    assert technical_complete(data)
    for owner in data:
        for table in data[owner]:
            original = data[owner][table]
            data[owner][table] = []
            assert not technical_complete(data)
            data[owner][table] = [{"state": "PENDING"}]
            assert not technical_complete(data)
            data[owner][table] = original


def test_coordinator_rejects_unknown_action_before_resources(tmp_path):
    from validation.functional.c_qualify import main

    target = tmp_path / "not-created"
    with pytest.raises(ValueError, match="INVALID_ACTIONS"):
        main(target, ("retry",))
    assert not target.exists()

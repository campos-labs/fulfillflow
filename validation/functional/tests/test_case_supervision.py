"""Check evidence export boundaries without invoking Docker or the product."""

import asyncio
import io
import json
import sys

import pytest
from case_b import ScenarioExportError, capture_output, environment, launch_worker


class OutputProcess:
    def __init__(self, data):
        self.stdout = io.BytesIO(data)


def test_process_capture_preserves_controlled_probe_error_without_external_text():
    child = OutputProcess(
        b'{"marker":"probe_exit","error":{"stage":"probe","exception_type":"PermissionError",'
        b'"code":"probe_source_mismatch","errno":13,"message":"secret",'
        b'"token":"private","filename":"sensitive"}}\n'
        b"unsanitized response body secret\n"
    )
    result = asyncio.run(capture_output(child))
    assert result["lines"] == 2
    assert result["diagnostics"] == [
        {
            "marker": "probe_exit",
            "origin": None,
            "error": {
                "stage": "probe",
                "exception_type": "PermissionError",
                "code": "probe_source_mismatch",
                "errno": 13,
            },
        }
    ]
    assert "secret" not in str(result)
    assert "sensitive" not in str(result)
    assert result["raw_stream_exported"] is False


def test_child_environment_does_not_inherit_application_destinations(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "historical-database")
    monkeypatch.setenv("AMQP_URL", "historical-broker")
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", "historical-marker")
    actual = environment(tmp_path, tmp_path / "new", {"SERVICE_ROLE": "core"})
    assert "DATABASE_URL" not in actual
    assert "AMQP_URL" not in actual
    assert "WORKER_HEARTBEAT_PATH" not in actual
    assert actual["FUNCTIONAL_APPLICATION_SOURCE"] == str(tmp_path)


def test_export_exception_retains_primary_and_cleanup_diagnostics():
    result = {
        "errors": [{"code": "primary"}],
        "cleanup_errors": [{"code": "cleanup"}],
        "export_errors": [{"code": "export"}],
    }
    error = ScenarioExportError(result)
    assert error.result is result
    assert str(error) == "SCENARIO_EXPORT_FAILED"


def test_native_worker_resolves_frozen_config_but_keeps_temporary_output_isolated(tmp_path):
    source = tmp_path / "source with spaces"
    output = tmp_path / "case output"
    source.mkdir()
    output.mkdir()
    (source / "alembic_core.ini").write_text("frozen-config", encoding="utf-8")
    code = (
        "import json,os; from pathlib import Path; "
        "import sys,sqlalchemy; print(json.dumps({'cwd':str(Path.cwd()),'temp':os.environ['TEMP'],"
        "'pid':os.getpid(),'prefix':sys.prefix,'sqlalchemy':sqlalchemy.__file__,"
        "'config':Path('alembic_core.ini').read_text()}))"
    )
    process = launch_worker(["-c", code], source, output, {})
    data, _ = process.communicate(timeout=10)
    assert process.returncode == 0
    import sqlalchemy

    assert json.loads(data) == {
        "cwd": str(source),
        "temp": str(output),
        "config": "frozen-config",
        "pid": process.pid,
        "prefix": sys.prefix,
        "sqlalchemy": sqlalchemy.__file__,
    }


def test_worker_refuses_implicit_dotenv_before_process_start(tmp_path):
    (tmp_path / ".env").write_text("DATABASE_URL=historical", encoding="utf-8")
    with pytest.raises(ValueError, match="APPLICATION_DOTENV_NOT_ALLOWED"):
        launch_worker(["-c", "raise SystemExit(99)"], tmp_path, tmp_path, {})

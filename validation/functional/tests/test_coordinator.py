"""Coordinator checks with fake scenarios/dependencies; no Docker or product workload."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import fromstring

import pytest
import run_b


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    folder = tmp_path / "tool"
    folder.mkdir()
    (folder / "README.md").write_text("frozen protocol\n", encoding="utf-8")
    digest = hashlib.sha256((folder / "README.md").read_bytes()).hexdigest()
    provenance = {"tool": {"files_sha256": {"README.md": digest}}}
    state: dict[str, Any] = {
        "folder": folder,
        "provenance": provenance,
        "calls": [],
        "statuses": [],
        "setup_error": None,
        "shutdown_error": None,
        "logs_error": None,
    }

    class FakeInfrastructure:
        def __init__(self, _run_id: str, _output: Path, **options: str) -> None:
            state["infra_options"] = options
            self.metadata = {"owned": True}
            state["calls"].append("construct")

        async def start(self) -> dict[str, Any]:
            state["calls"].append("start")
            assert (folder / "results" / "test-01" / "package" / "README.md").read_bytes() == (
                folder / "README.md"
            ).read_bytes()
            if state["setup_error"]:
                raise state["setup_error"]
            return {}

        async def stop(self) -> list[dict[str, Any]]:
            state["calls"].append("stop")
            if state["shutdown_error"]:
                raise state["shutdown_error"]
            return []

        async def export_logs(self) -> dict[str, Any]:
            state["calls"].append("logs")
            if state["logs_error"]:
                raise state["logs_error"]
            return {}

    async def scenario(
        _source: Path,
        _infra: Any,
        _info: Any,
        _output: Path,
        owner: str,
        action: str,
        repetition: int,
    ) -> dict[str, Any]:
        state["calls"].append((owner, action, repetition))
        value = state["statuses"].pop(0) if state["statuses"] else "PASS"
        if isinstance(value, BaseException):
            raise value
        return {
            "status": value,
            "errors": [] if value == "PASS" else [{"code": "BROKEN_INVARIANT"}],
            "cleanup_errors": [],
            "export_errors": [],
        }

    monkeypatch.setattr(run_b, "FOLDER", folder)
    monkeypatch.setattr(run_b, "capture", lambda _source, _folder: provenance)
    monkeypatch.setattr(run_b, "Infrastructure", FakeInfrastructure)
    monkeypatch.setattr(run_b, "scenario", scenario)
    return state


def arguments(**extra: Any) -> argparse.Namespace:
    return argparse.Namespace(
        **{
            "source": Path("frozen-source"),
            "run_id": "test-01",
            "mode": "development",
            "owner": "all",
            "repetitions": 3,
            **extra,
        }
    )


def summary(harness: dict[str, Any]) -> dict[str, Any]:
    return json.loads((harness["folder"] / "results/test-01/summary.json").read_text())


async def test_evaluated_mode_is_blocked_before_creating_any_result(
    harness: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="EVALUATED_PACKAGE_NOT_RELEASED"):
        await run_b.execute(arguments(mode="evaluated"))
    assert not (harness["folder"] / "results").exists()
    assert harness["calls"] == []


@pytest.mark.parametrize("run_id", ["../old", "UPPER", "x" * 41, ""])
async def test_invalid_destination_never_starts_infrastructure(
    harness: dict[str, Any],
    run_id: str,
) -> None:
    with pytest.raises(ValueError, match="INVALID_RUN_ID"):
        await run_b.execute(arguments(run_id=run_id))
    assert harness["calls"] == []


async def test_existing_destination_is_not_overwritten(harness: dict[str, Any]) -> None:
    output = harness["folder"] / "results/test-01"
    output.mkdir(parents=True)
    sentinel = output / "old.json"
    sentinel.write_bytes(b"preserved")
    with pytest.raises(FileExistsError):
        await run_b.execute(arguments())
    assert sentinel.read_bytes() == b"preserved"
    assert harness["calls"] == []


async def test_all_cases_execute_in_predefined_order_and_remain_development(
    harness: dict[str, Any],
) -> None:
    assert await run_b.execute(arguments()) == 0
    report = summary(harness)
    assert report["status"] == "PASS"
    assert report["eligible_for_evaluation"] is False
    assert report["mode"] == "development"
    assert [call for call in harness["calls"] if isinstance(call, tuple)] == run_b.cases("all", 3)
    assert report["not_run"] == []
    output = harness["folder"] / "results/test-01"
    xml = fromstring((output / "junit.xml").read_bytes())
    assert xml.attrib["tests"] == "13"
    assert xml.attrib["errors"] == "0"
    assert xml.attrib["failures"] == "0"
    for line in (output / "checksums.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    "status, tag", [("FAIL", "failure"), ("INVALID", "error"), ("INCONCLUSIVE", "error")]
)
async def test_first_unsuccessful_case_stops_sequence_and_emits_remaining_skips(
    harness: dict[str, Any],
    status: str,
    tag: str,
) -> None:
    harness["statuses"] = ["PASS", status]
    assert await run_b.execute(arguments()) == 2
    report = summary(harness)
    assert report["status"] == status
    assert [result["status"] for result in report["cases"]] == ["PASS", status] + ["NOT_RUN"] * 10
    assert len([call for call in harness["calls"] if isinstance(call, tuple)]) == 2
    xml = fromstring((harness["folder"] / "results/test-01/junit.xml").read_bytes())
    assert len(xml.findall("testcase/skipped")) == 10
    assert len(xml.findall(f"testcase/{tag}")) == 1


async def test_setup_failure_is_separate_from_twelve_not_run_cases(
    harness: dict[str, Any],
) -> None:
    harness["setup_error"] = PermissionError(13, "secret DSN must not appear")
    assert await run_b.execute(arguments()) == 2
    report = summary(harness)
    assert report["setup"]["status"] == "INVALID"
    assert len(report["not_run"]) == 12
    assert report["errors"][0]["errno"] == 13
    assert harness["calls"] == ["construct", "start", "stop", "logs"]
    assert "secret" not in json.dumps(report)
    xml = fromstring((harness["folder"] / "results/test-01/junit.xml").read_bytes())
    assert xml.attrib["errors"] == "1"
    assert xml.attrib["skipped"] == "12"


async def test_source_failure_never_creates_or_stops_infrastructure(
    harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failure(*_args: Any) -> None:
        raise ValueError("WRONG_APPLICATION_HEAD")

    monkeypatch.setattr(run_b, "capture", failure)
    assert await run_b.execute(arguments()) == 2
    assert harness["calls"] == []
    assert summary(harness)["setup"]["status"] == "INVALID"
    assert summary(harness)["errors"][0]["code"] == "WRONG_APPLICATION_HEAD"


async def test_archive_mismatch_prevents_infrastructure_start(harness: dict[str, Any]) -> None:
    (harness["folder"] / "README.md").write_text("changed after capture")
    assert await run_b.execute(arguments()) == 2
    assert harness["calls"] == []


@pytest.mark.parametrize(
    "relative", ["../escape.py", "results/old.txt", ".artifacts/cache", "C:/x"]
)
def test_archive_rejects_generated_or_external_paths(
    harness: dict[str, Any],
    tmp_path: Path,
    relative: str,
) -> None:
    provenance = {"tool": {"files_sha256": {relative: "a" * 64}}}
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(ValueError, match="TOOL_PACKAGE_PATH_INVALID"):
        run_b.archive_tool(harness["folder"], output, provenance)


async def test_shutdown_and_export_causes_do_not_replace_case_failure(
    harness: dict[str, Any],
) -> None:
    harness["statuses"] = ["FAIL"]
    harness["shutdown_error"] = PermissionError(13, "private shutdown path")
    harness["logs_error"] = OSError(5, "private docker log")
    assert await run_b.execute(arguments()) == 2
    report = summary(harness)
    assert report["status"] == "FAIL"
    assert report["cases"][0]["errors"] == [{"code": "BROKEN_INVARIANT"}]
    assert report["shutdown_errors"][0]["errno"] == 13
    assert report["export_errors"][0]["errno"] == 5
    assert harness["calls"][-2:] == ["stop", "logs"]
    assert "private" not in json.dumps(report)


async def test_cleanup_failure_after_passes_makes_report_and_junit_nonzero(
    harness: dict[str, Any],
) -> None:
    harness["shutdown_error"] = TimeoutError("sensitive")
    assert await run_b.execute(arguments(owner="core", repetitions=1)) == 2
    assert summary(harness)["status"] == "INCONCLUSIVE"
    xml = fromstring((harness["folder"] / "results/test-01/junit.xml").read_bytes())
    assert xml.attrib["errors"] == "1"
    assert xml.find("testcase[@name='LIFECYCLE']/error") is not None


async def test_summary_export_failure_stderr_preserves_all_failure_channels(
    harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_write = run_b.write_json

    def failing_summary(path: Path, value: Any) -> None:
        if path.name == "summary.json":
            raise PermissionError(13, "sensitive summary destination")
        original_write(path, value)

    monkeypatch.setattr(run_b, "write_json", failing_summary)
    harness["statuses"] = ["FAIL"]
    harness["shutdown_error"] = OSError(5, "sensitive")
    harness["logs_error"] = TimeoutError("sensitive")
    assert await run_b.execute(arguments()) == 2
    fallback = json.loads(capsys.readouterr().err)
    assert fallback["cases"][0]["primary"] == [{"code": "BROKEN_INVARIANT"}]
    assert fallback["shutdown"][0]["errno"] == 5
    assert len(fallback["export"]) == 2
    assert fallback["export"][-1]["artifact"] == "summary"
    assert "sensitive" not in json.dumps(fallback)
    assert (harness["folder"] / "results/test-01/junit.xml").is_file()
    assert (harness["folder"] / "results/test-01/checksums.sha256").is_file()


async def test_unexpected_scenario_exit_is_inconclusive_and_never_retried(
    harness: dict[str, Any],
) -> None:
    harness["statuses"] = [RuntimeError("sensitive")]
    assert await run_b.execute(arguments()) == 2
    report = summary(harness)
    assert report["cases"][0]["status"] == "INCONCLUSIVE"
    assert len(report["not_run"]) == 11
    assert report["errors"][0]["exception_type"] == "RuntimeError"
    assert "sensitive" not in json.dumps(report)


async def test_partial_scenario_export_error_keeps_original_and_secondary_causes(
    harness: dict[str, Any],
) -> None:
    class ScenarioExportError(RuntimeError):
        def __init__(self) -> None:
            self.result = {
                "status": "FAIL",
                "errors": [{"code": "ORIGINAL_FAILURE"}],
                "cleanup_errors": [{"exception_type": "TimeoutError", "stage": "shutdown"}],
                "export_errors": [{"exception_type": "PermissionError", "stage": "export"}],
            }
            super().__init__("sensitive exception message")

    harness["statuses"] = [ScenarioExportError()]
    assert await run_b.execute(arguments()) == 2
    report = summary(harness)
    assert report["status"] == "FAIL"
    result = report["cases"][0]
    assert result["errors"][0] == {"code": "ORIGINAL_FAILURE"}
    assert result["cleanup_errors"][0]["stage"] == "shutdown"
    assert result["export_errors"][0]["stage"] == "export"
    assert len(report["not_run"]) == 11
    assert "sensitive" not in json.dumps(report)


def test_junit_failure_does_not_prevent_summary_or_checksums(
    harness: dict[str, Any],
    tmp_path: Path,
) -> None:
    output = tmp_path / "partial-output"
    output.mkdir()
    (output / "junit.xml").mkdir()
    report = {
        "status": "FAIL",
        "setup": {"id": "SETUP", "status": "PASS", "errors": []},
        "cases": [],
        "errors": [{"code": "ORIGINAL_FAILURE"}],
        "shutdown_errors": [],
        "export_errors": [],
    }
    with pytest.raises(run_b.ExportFailure):
        run_b.export(output, report)
    saved = json.loads((output / "summary.json").read_text())
    assert saved["errors"] == [{"code": "ORIGINAL_FAILURE"}]
    assert saved["export_errors"][0]["artifact"] == "junit"
    assert (output / "checksums.sha256").is_file()


@pytest.mark.parametrize("subnet", [None, "", "10.254.240.0/28"])
async def test_explicit_network_subnet_forwarded_only_when_nonempty(
    harness: dict[str, Any],
    subnet: str | None,
) -> None:
    assert await run_b.execute(arguments(owner="core", repetitions=1, network_subnet=subnet)) == 0
    assert harness["infra_options"] == ({"network_subnet": subnet} if subnet else {})


def released_arguments(harness: dict[str, Any], **extra: Any) -> argparse.Namespace:
    path = harness["folder"] / "release.json"
    release = {
        "schema": 1,
        "run_id": "test-01",
        "network_subnet": "10.254.240.32/28",
        "case_order": [list(case) for case in run_b.cases("all", 3)],
        "provenance": harness["provenance"],
    }
    path.write_text(json.dumps(release), encoding="utf-8")
    return arguments(
        mode="evaluated",
        release_file=path,
        release_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        network_subnet="10.254.240.32/28",
        **extra,
    )


async def test_evaluated_release_runs_exact_twelve_cases(harness: dict[str, Any]) -> None:
    args = released_arguments(harness)
    assert await run_b.execute(args) == 0
    report = summary(harness)
    assert report["eligible_for_evaluation"] is True
    assert report["mode"] == "evaluated"
    assert report["release_sha256"] == args.release_sha256
    assert len(report["cases"]) == 12
    output = harness["folder"] / "results/test-01"
    assert json.loads((output / "release.json").read_text())["provenance"] == harness["provenance"]
    assert fromstring((output / "junit.xml").read_bytes()).get("name") == "functional-b-evaluated"


@pytest.mark.parametrize(
    "field",
    ["hash", "run_id", "network_subnet", "case_order", "provenance", "owner", "repetitions"],
)
async def test_evaluated_release_rejects_mismatch_before_dependencies(
    harness: dict[str, Any],
    field: str,
) -> None:
    args = released_arguments(harness)
    if field == "hash":
        args.release_sha256 = "0" * 64
    elif field in {"owner", "repetitions"}:
        setattr(args, field, "core" if field == "owner" else 1)
    else:
        release = json.loads(args.release_file.read_text())
        release[field] = {} if field == "provenance" else "changed"
        args.release_file.write_text(json.dumps(release))
        args.release_sha256 = hashlib.sha256(args.release_file.read_bytes()).hexdigest()
    assert await run_b.execute(args) == 2
    assert harness["calls"] == []
    assert summary(harness)["setup"]["status"] == "INVALID"


async def test_identity_change_after_case_stops_without_next_case(
    harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = released_arguments(harness)
    calls = 0

    def capture(*_args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return harness["provenance"] if calls < 3 else {"changed": True}

    monkeypatch.setattr(run_b, "capture", capture)
    assert await run_b.execute(args) == 2
    report = summary(harness)
    assert report["cases"][0]["status"] == "INCONCLUSIVE"
    assert report["errors"][0]["code"] == "EVALUATED_IDENTITY_CHANGED"
    assert len(report["not_run"]) == 11
    assert len([x for x in harness["calls"] if isinstance(x, tuple)]) == 1


async def test_evaluated_failure_is_preserved_without_replacement(harness: dict[str, Any]) -> None:
    args = released_arguments(harness)
    harness["statuses"] = ["PASS", "FAIL"]
    assert await run_b.execute(args) == 2
    assert [x["status"] for x in summary(harness)["cases"]] == ["PASS", "FAIL"] + ["NOT_RUN"] * 10

"""Bounded functional verification coordinator; not a load generator."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from case_b import scenario
from environment import Infrastructure, InfrastructureError
from provenance import capture

from fulfillflow.asyncio_support import run_async
from support import controlled_error, write_json

FOLDER = Path(__file__).resolve().parent
_STATUSES = frozenset({"PASS", "FAIL", "INCONCLUSIVE", "INVALID", "NOT_RUN", "NOT_APPLICABLE"})
_IDENTITY_CODES = frozenset(
    {
        "SOURCE_GIT_CHECK_FAILED",
        "PRODUCTION_DEPENDENCY_NOT_FROZEN",
        "DIRECT_DEPENDENCY_REFERENCE_NOT_SUPPORTED",
        "DEPENDENCY_REQUIREMENT_NOT_MET",
        "WRONG_APPLICATION_HEAD",
        "WRONG_APPLICATION_TAG",
        "FROZEN_TRACKED_FILES_CHANGED",
        "UNTRACKED_APPLICATION_MODULE",
        "PYTHON_313_REQUIRED",
        "APPLICATION_IMPORT_SOURCE_MISMATCH",
        "DUPLICATE_INSTALLED_DISTRIBUTION",
        "INSTALLED_PACKAGE_NOT_IN_LOCK",
        "INSTALLED_VERSION_DIFFERS_FROM_LOCK",
        "TOOL_PACKAGE_EMPTY",
        "TOOL_PACKAGE_PATH_INVALID",
        "TOOL_PACKAGE_CHANGED",
        "SCENARIO_RESULT_INVALID",
        "EVIDENCE_PATH_INVALID",
        "INVALID_RUN_ID",
        "INVALID_CASE_SELECTION",
        "EVALUATED_PACKAGE_NOT_RELEASED",
        "EVALUATED_RELEASE_MISMATCH",
        "EVALUATED_IDENTITY_CHANGED",
    }
)


class ExportFailure(RuntimeError):
    """Export failed after independently attempting the remaining evidence files."""


def cases(owner: str, repetitions: int) -> list[tuple[str, str, int]]:
    if owner not in {"core", "tracking", "all"} or repetitions not in {1, 2, 3}:
        raise ValueError("INVALID_CASE_SELECTION")
    return [
        (role, action, repetition)
        for role in (("core", "tracking") if owner == "all" else (owner,))
        for repetition in range(1, repetitions + 1)
        for action in ("control", "kill")
    ]


def _error(exc: BaseException, stage: str) -> dict[str, Any]:
    if isinstance(exc, InfrastructureError):
        return exc.as_dict()
    result = controlled_error(exc, stage)
    if isinstance(exc, ValueError) and len(exc.args) == 1 and isinstance(exc.args[0], str):
        if exc.args[0] in _IDENTITY_CODES:
            result["code"] = exc.args[0]
    return result


def archive_tool(folder: Path, output: Path, provenance: dict[str, Any]) -> None:
    """Archive precisely the captured tool bytes, never recurse into generated outputs."""
    inventory = provenance["tool"]["files_sha256"]
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError("TOOL_PACKAGE_EMPTY")
    package = output / "package"
    package.mkdir(exist_ok=False)
    for relative, digest in sorted(inventory.items()):
        path = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or ":" in relative
            or path.is_absolute()
            or any(part in {"..", "results"} or part.startswith(".") for part in path.parts)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("TOOL_PACKAGE_PATH_INVALID")
        source = folder / path
        if not source.resolve().is_relative_to(folder.resolve()) or source.is_symlink():
            raise ValueError("TOOL_PACKAGE_PATH_INVALID")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("TOOL_PACKAGE_CHANGED")
        target = package / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)


def _diagnostics(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report["status"],
        "primary": report["errors"],
        "shutdown": report["shutdown_errors"],
        "export": report["export_errors"],
        "cases": [
            {
                "id": result["id"],
                "status": result["status"],
                "primary": result.get("errors", []),
                "shutdown": result.get("cleanup_errors", []),
                "export": result.get("export_errors", []),
            }
            for result in report["cases"]
        ],
    }


def _junit(report: dict[str, Any]) -> bytes:
    entries = [report["setup"], *report["cases"]]
    suite = Element(
        "testsuite",
        name=f"functional-b-{report.get('mode', 'development')}",
        tests=str(len(entries)),
        failures=str(sum(entry["status"] == "FAIL" for entry in entries)),
        errors=str(sum(entry["status"] in {"INVALID", "INCONCLUSIVE"} for entry in entries)),
        skipped=str(sum(entry["status"] in {"NOT_RUN", "NOT_APPLICABLE"} for entry in entries)),
    )
    for result in entries:
        case = SubElement(
            suite,
            "testcase",
            name=result["id"],
            classname="functional.b",
            time=str(result.get("elapsed_seconds", 0)),
        )
        status = result["status"]
        if status in {"NOT_RUN", "NOT_APPLICABLE"}:
            SubElement(case, "skipped", message=result["reason"])
        elif status != "PASS":
            tag = "failure" if status == "FAIL" else "error"
            SubElement(case, tag, message=status).text = json.dumps(
                {
                    "primary": result.get("errors", []),
                    "shutdown": result.get("cleanup_errors", []),
                    "export": result.get("export_errors", []),
                },
                sort_keys=True,
            )
    # Lifecycle failure may occur after every case passed; do not emit green JUnit.
    lifecycle = [*report["shutdown_errors"], *report["export_errors"]]
    if lifecycle:
        case = SubElement(suite, "testcase", name="LIFECYCLE", classname="functional.b")
        SubElement(case, "error", message="INCONCLUSIVE").text = json.dumps(lifecycle)
        suite.set("tests", str(len(entries) + 1))
        suite.set("errors", str(int(suite.get("errors", "0")) + 1))
    return bytes(tostring(suite, encoding="utf-8", xml_declaration=True))


def export(output: Path, report: dict[str, Any]) -> None:
    """Attempt every export independently; preserve sanitized primary/secondary causes."""
    failure_count = len(report["export_errors"])
    try:
        with (output / "junit.xml").open("xb") as stream:
            stream.write(_junit(report))
    except BaseException as exc:
        report["export_errors"].append({"artifact": "junit", **_error(exc, "export")})
    if report["export_errors"] and report["status"] == "PASS":
        report["status"] = "INCONCLUSIVE"
    try:
        write_json(output / "summary.json", report)
    except BaseException as exc:
        report["export_errors"].append({"artifact": "summary", **_error(exc, "export")})
    try:
        hashes = []
        for path in sorted(output.rglob("*")):
            if (
                path.is_file()
                and path.name != "checksums.sha256"
                and not path.name.startswith(".functional-")
            ):
                if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
                    raise ValueError("EVIDENCE_PATH_INVALID")
                hashes.append(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                    f"{path.relative_to(output).as_posix()}"
                )
        with (output / "checksums.sha256").open("x", encoding="utf-8") as stream:
            stream.write("\n".join(hashes) + "\n")
    except BaseException as exc:
        report["export_errors"].append({"artifact": "checksums", **_error(exc, "export")})
    if len(report["export_errors"]) > failure_count:
        if report["status"] == "PASS":
            report["status"] = "INCONCLUSIVE"
        raise ExportFailure("EVIDENCE_EXPORT_FAILED")


def _planned(owner: str, repetitions: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"B-{role.upper()}-{action}-{repetition}",
            "owner": role,
            "action": action,
            "repetition": repetition,
            "status": "NOT_RUN",
            "reason": "sequence_not_reached",
            "errors": [],
        }
        for role, action, repetition in cases(owner, repetitions)
    ]


def verify_release(args: argparse.Namespace, provenance: dict[str, Any]) -> dict[str, Any]:
    """Bind one fixed twelve-case run to reviewed bytes and separate source identity."""
    path = Path(args.release_file)
    raw = path.read_bytes()
    digest = args.release_sha256
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("EVALUATED_RELEASE_MISMATCH")
    release = json.loads(raw)
    if (
        not isinstance(release, dict)
        or release.get("schema") != 1
        or release.get("run_id") != args.run_id
        or not getattr(args, "network_subnet", None)
        or release.get("network_subnet") != args.network_subnet
        or args.owner != "all"
        or args.repetitions != 3
        or release.get("case_order") != [list(case) for case in cases("all", 3)]
    ):
        raise ValueError("EVALUATED_RELEASE_MISMATCH")
    if release.get("provenance") != provenance:
        raise ValueError("EVALUATED_IDENTITY_CHANGED")
    return release


async def execute(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.run_id):
        raise ValueError("INVALID_RUN_ID")
    if args.mode not in {"development", "evaluated"}:
        raise ValueError("INVALID_CASE_SELECTION")
    evaluated = args.mode == "evaluated"
    if evaluated and (
        not getattr(args, "release_file", None) or not getattr(args, "release_sha256", None)
    ):
        raise ValueError("EVALUATED_PACKAGE_NOT_RELEASED")
    planned = _planned(args.owner, args.repetitions)
    output = FOLDER / "results" / args.run_id
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "mode": args.mode,
        "eligible_for_evaluation": False,
        "run_id": args.run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "INVALID",
        "setup": {"id": "SETUP", "status": "INVALID", "errors": []},
        "cases": planned,
        "errors": [],
        "shutdown_errors": [],
        "export_errors": [],
    }
    infra: Infrastructure | None = None
    current: dict[str, Any] | None = None
    try:
        provenance = await asyncio.to_thread(capture, args.source, FOLDER)
        if evaluated:
            release = await asyncio.to_thread(verify_release, args, provenance)
            write_json(output / "release.json", release)
            report["release_sha256"] = args.release_sha256
            report["eligible_for_evaluation"] = True
        await asyncio.to_thread(archive_tool, FOLDER, output, provenance)
        write_json(output / "provenance.json", provenance)
        network_subnet = getattr(args, "network_subnet", None)
        if network_subnet:
            infra = Infrastructure(args.run_id, output / "infra", network_subnet=network_subnet)
        else:
            infra = Infrastructure(args.run_id, output / "infra")
        info = await infra.start()
        write_json(output / "infrastructure.json", infra.metadata)
        report["setup"]["status"] = "PASS"
        for current in planned:
            owner, action, repetition = (current["owner"], current["action"], current["repetition"])
            if evaluated:
                current_identity = await asyncio.to_thread(capture, args.source, FOLDER)
                await asyncio.to_thread(verify_release, args, current_identity)
            print(f"Starting B-{owner} {action} r{repetition}; no load.", flush=True)
            result = await scenario(
                args.source,
                infra,
                info,
                output / f"{owner}-{repetition}-{action}",
                owner,
                action,
                repetition,
            )
            if result.get("status") not in _STATUSES - {"NOT_RUN", "NOT_APPLICABLE"}:
                raise ValueError("SCENARIO_RESULT_INVALID")
            current.update(result)
            if evaluated:
                current_identity = await asyncio.to_thread(capture, args.source, FOLDER)
                await asyncio.to_thread(verify_release, args, current_identity)
            current.pop("reason", None)
            print(f"B-{owner} {action} r{repetition}: {current['status']}", flush=True)
            if current["status"] != "PASS":
                report["status"] = current["status"]
                break
        else:
            report["status"] = "PASS"
    except BaseException as exc:
        error = _error(exc, "supervision")
        report["errors"].append(error)
        if current is None:
            report["setup"]["errors"].append(error)
        else:
            partial = getattr(exc, "result", None)
            if isinstance(partial, dict):
                current.update(partial)
            if current["status"] not in {"FAIL", "INVALID"}:
                current["status"] = "INCONCLUSIVE"
            current["reason"] = "scenario_did_not_return"
            current.setdefault("errors", []).append(error)
            report["status"] = current["status"]
    finally:
        if infra is not None:
            try:
                report["shutdown_errors"].extend(await infra.stop())
            except BaseException as exc:
                report["shutdown_errors"].append(_error(exc, "shutdown"))
            # Export is attempted even when shutdown failed.
            try:
                logs = await infra.export_logs()
                for role, detail in logs.items():
                    if isinstance(detail, dict) and "error" in detail:
                        report["export_errors"].append(
                            {"artifact": "dependency_lifecycle", "role": role, **detail["error"]}
                        )
            except BaseException as exc:
                report["export_errors"].append(_error(exc, "export"))
        if (report["shutdown_errors"] or report["export_errors"]) and report["status"] == "PASS":
            report["status"] = "INCONCLUSIVE"
        report["ended_at"] = datetime.now(UTC).isoformat()
        report["not_run"] = [case["id"] for case in planned if case["status"] == "NOT_RUN"]
        try:
            await asyncio.to_thread(export, output, report)
        except BaseException as exc:
            if not isinstance(exc, ExportFailure):
                report["export_errors"].append(_error(exc, "export"))
            print(json.dumps(_diagnostics(report)), file=sys.stderr)
            return 2
    print(json.dumps({"status": report["status"], "report": str(output / "summary.json")}))
    return 0 if report["status"] == "PASS" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--network-subnet", default=None)
    parser.add_argument("--release-file", type=Path)
    parser.add_argument("--release-sha256")
    parser.add_argument("--mode", choices=("development", "evaluated"), default="development")
    parser.add_argument("--owner", choices=("core", "tracking", "all"), default="all")
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    try:
        return run_async(execute(args))
    except BaseException as exc:
        print(json.dumps(_error(exc, "preflight")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

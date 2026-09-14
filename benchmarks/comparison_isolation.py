"""Manual execution only: export and stop precisely inventoried prior resources."""

import json
import subprocess
from pathlib import Path
from typing import Any

from benchmarks.active_screen_energy import require_energy
from benchmarks.controls_v10 import ControlError, _sha256, _write_checksums
from benchmarks.operational_errors import error_report, sanitize, write_report
from benchmarks.paired_controls import verify_checksums

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "fulfillflow-comparison-active-01-v10"
HISTORY = ROOT / "benchmarks/results/reviewed-comparison120-mixed-4-win9445-01-active-c1-v10-v10"


def capture(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )


def inspect(identifier: str) -> dict[str, Any]:
    d = json.loads(capture(["inspect", identifier]).stdout)[0]
    return {
        "id": d["Id"],
        "name": d["Name"],
        "image": d["Image"],
        "project": d["Config"]["Labels"].get("com.docker.compose.project"),
        "mounts": sorted(d["Mounts"], key=lambda m: m["Destination"]),
        "running": d["State"]["Running"],
        "exit_code": d["State"]["ExitCode"],
        "oom": d["State"]["OOMKilled"],
    }


def prepare_isolation(package: Path) -> None:
    verify_checksums(HISTORY)
    owned = json.loads((HISTORY / "preparation/owned.json").read_text(encoding="utf-8"))
    if owned != {
        "project": PROJECT,
        "source": str(ROOT / "benchmarks/results/comparison-win9445-v10-source-01"),
    }:
        raise ControlError("preserved ownership differs")
    recorded = [
        json.loads(line)
        for line in (HISTORY / "diagnostics/containers.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    ids = [old["ID"] for old in recorded if old.get("Service") in ("app", "db")]
    rows = [inspect(i) for i in ids]
    if len(rows) != 2 or any(r["project"] != PROJECT for r in rows):
        raise ControlError("expected exactly the two preserved prior campaign resources")
    rows.sort(key=lambda r: r["name"])
    if [r["name"] for r in rows] != [f"/{PROJECT}-app-1", f"/{PROJECT}-db-1"]:
        raise ControlError("prior resource roles differ")
    for row in rows:
        if (
            sum(
                row["id"].startswith(old["ID"]) and row["name"] == "/" + old["Name"]
                for old in recorded
            )
            != 1
        ):
            raise ControlError("container differs from the preserved execution inventory")
    if set(capture(["ps", "--quiet", "--no-trunc"]).stdout.split()) != {
        row["id"] for row in rows if row["running"]
    }:
        raise ControlError("unexpected concurrent resources")
    write_report(
        package / "isolation.json",
        {
            "containers": rows,
            "preserved": {str(p): _sha256(p) for p in sorted(HISTORY.rglob("*")) if p.is_file()},
            "method": "export all logs then docker stop --time 30; no removal or restart",
            "volatile_state_lost": True,
        },
    )


def isolate(package: Path, destination: Path) -> None:
    plan = json.loads((package / "isolation.json").read_text(encoding="utf-8"))
    if destination.exists():
        raise ControlError("isolation destination exists; no retry")
    destination.mkdir(parents=True)
    report: dict[str, Any] = {"complete": False, "stopped": [], "load_executed": False}
    try:
        for path, digest in plan["preserved"].items():
            if _sha256(Path(path)) != digest:
                raise ControlError("preserved prior evidence changed")
        rows = [inspect(c["id"]) for c in plan["containers"]]
        write_report(destination / "before.json", rows)
        if rows != plan["containers"]:
            raise ControlError("prior container identity or state changed; no stop performed")
        if set(capture(["ps", "--quiet", "--no-trunc"]).stdout.split()) != {
            r["id"] for r in rows if r["running"]
        }:
            raise ControlError("unexpected concurrent resources; no stop performed")
        for i, row in enumerate(rows):
            require_energy()
            logs = capture(["logs", "--timestamps", "--tail", "10000", row["id"]])
            write_report(
                destination / f"logs-{i}.json",
                {
                    "exit_code": logs.returncode,
                    "stdout": sanitize(logs.stdout),
                    "stderr": sanitize(logs.stderr),
                },
            )
        for i, row in enumerate(rows):
            require_energy()
            if inspect(row["id"]) != row:
                raise ControlError("resource changed after export; stop sequence interrupted")
            stopped = capture(["stop", "--time", "30", row["id"]]) if row["running"] else None
            after = inspect(row["id"])
            logs = capture(["logs", "--timestamps", "--tail", "100", row["id"]])
            write_report(
                destination / f"stop-{i}.json",
                {
                    "exit_code": stopped.returncode if stopped else None,
                    "already_stopped": stopped is None,
                    "state": after,
                    "stdout": sanitize(logs.stdout),
                    "stderr": sanitize(logs.stderr),
                },
            )
            expected = (
                "database system is shut down"
                if row["name"].endswith("-db-1")
                else "Application shutdown complete."
            )
            unchanged = all(
                after[k] == row[k] for k in ("id", "name", "image", "project", "mounts")
            )
            if (
                not unchanged
                or after["running"]
                or after["oom"]
                or after["exit_code"] not in (0, 143)
                or expected not in logs.stdout + logs.stderr
            ):
                raise ControlError("graceful shutdown unconfirmed; preserve resources and stop")
            report["stopped"].append(row["id"])
        report["complete"] = True
    except Exception as exc:
        report["error"] = error_report(exc)
        raise
    finally:
        write_report(destination / "result.json", report)
        _write_checksums(destination)

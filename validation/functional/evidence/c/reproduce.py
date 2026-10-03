"""Verify distributed hashes and reconstruct the evaluated matrix without Docker."""

import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


def require(value: bool, label: str) -> None:
    if not value:
        raise ValueError(label)


def verify(base: Path) -> dict[str, Any]:
    package = json.loads((base / "packages.json").read_text(encoding="utf-8"))
    data = {}
    originals = {}
    for name, record in package["archives"].items():
        raw = (base / name).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == record["sha256"], "archive hash")
        with zipfile.ZipFile(base / name) as archive:
            require(
                set(archive.namelist()) == {e["path"] for e in record["entries"]}, "archive entries"
            )
            require(len(archive.namelist()) == len(record["entries"]), "duplicate entries")
            for entry in record["entries"]:
                body = archive.read(entry["path"])
                require(
                    hashlib.sha256(body).hexdigest() == entry["distributed_sha256"], "entry hash"
                )
                data[entry["path"]] = body
                originals[entry["path"]] = entry["original_sha256"]

    def read(name: str) -> dict[str, Any]:
        value = json.loads(data[name])
        if not isinstance(value, dict):
            raise ValueError("record must be an object")
        return value

    prep = ".artifacts/c-evaluated-preparation-02/"
    manifest = read(prep + "manifest.json")
    release = read(prep + "release.json")
    require(
        release == {"approved": True, "manifest_sha256": originals[prep + "manifest.json"]},
        "release",
    )
    require(manifest["protocol_sha256"] == originals[prep + "protocol.md"], "protocol origin")
    summary = read("results/c-evaluated-01/summary.json")
    require(summary["complete"] is True and len(summary["cases"]) == 54, "complete sequence")
    require([x["case"] for x in summary["cases"]] == manifest["cases"], "sequence")
    counts: Counter[tuple[str, str, str]] = Counter()
    for entry in summary["cases"]:
        item = entry["case"]
        version, action = item["version"], item["action"]
        folder = "results/c-evaluated-01/" + item["destination"] + "/"
        case = folder + version + "-" + action + "/"
        result = read(case + "result.json")
        group = read(folder + "summary.json")
        process = read(folder + version + "-" + action + "-supervision.json")
        require(
            result["mode"] == "evaluated"
            and result["version"] == version
            and result["action"] == action,
            "case identity",
        )
        require(result["status"] == entry["classification"], "classification")
        require(
            not result["cleanup_errors"] and "error" not in result and "error" not in group,
            "case error",
        )
        require(
            process["tree_empty"]
            and not process["timed_out"]
            and not process["forced_tree_cleanup"],
            "supervision",
        )
        require(
            len(group["cleanup"]) == 2
            and all(
                not x["running"] and not x["oom"] and x["exit_code"] == 0 for x in group["cleanup"]
            ),
            "container cleanup",
        )
        for name, digest in read(folder + "tool-hashes.json").items():
            require(
                digest == manifest["tools"][name] == originals[folder + "tool-package/" + name],
                "tool origin",
            )
        calls = result["webhook_calls"]
        if action == "unavailable":
            require(
                result["status"] == "INCONCLUSIVE"
                and process["exit_code"] == 2
                and result["outcome"] == "pending_at_limit_no_redelivery",
                "C4 outcome",
            )
            require(len(calls) == 1, "C4 no redelivery")
            require(
                30 <= read(case + "restoration-schedule.json")["actual_seconds_from_offer"] <= 32,
                "restoration schedule",
            )
            require(
                all(
                    x["select_one"] == 1
                    for x in read(case + "authenticated-readiness.json").values()
                ),
                "SQL ready",
            )
            state = read(case + "after-dependency-restore.json")
            obs = read(case + "request-completion.json")
            require(
                any(
                    not x["finished"] and x["request_id"] == calls[0]["request_id"]
                    for owner in obs.values()
                    for x in owner["requests"]
                ),
                "pending request",
            )
            core = state["core"]
            tracking = state.get("tracking", core)
            require(
                not tracking["inbox"]
                and not tracking["timeline"]
                and not core["notifications"]
                and not core.get("receipts", []),
                "no admission or effects",
            )
        else:
            require(result["status"] == "PASS" and process["exit_code"] == 0, "PASS")
            final = read(case + "final.json")
            core = final["core"]
            tracking = final.get("tracking", core)
            require(
                len(tracking["inbox"]) == 1 and tracking["inbox"][0]["status"] == "PROCESSED",
                "inbox",
            )
            require(
                len(tracking["timeline"]) == 1
                and tracking["timeline"][0]["application_result"] == "APPLIED",
                "timeline",
            )
            require(
                len(core["notifications"]) == 1
                and core["notifications"][0]["status"] == "SIMULATED",
                "notification",
            )
            require(
                core["shipments"][0]["status"] == "DELIVERED"
                and core["orders"][0]["status"] == "FULFILLED",
                "business completion",
            )
            if action in ("duplicate", "conflict"):
                require(
                    read(case + "initial-complete.json")
                    == read(case + "after-reoffer.json")
                    == final,
                    "unchanged effects",
                )
                require(
                    len(calls) == 2
                    and calls[-1]["http_status"] == (200 if action == "duplicate" else 409),
                    "reoffer",
                )
            if action == "kill":
                require(
                    result["kill"]["killed"]
                    and result["kill"]["pid"] == read(case + "barrier.json")["pid"],
                    "target PID",
                )
                require(
                    read(case + "after-kill.json") == read(case + "outside-barrier.json"),
                    "rollback",
                )
                require(result["recovery_action"] == "explicit_process_restart", "restart")
                wait = read(case + "without-redelivery.json")
                if version == "v1.2.0-rc.1":
                    require(wait["complete"] and len(calls) == 1, "async recovery")
                else:
                    require(
                        not wait["complete"] and wait["elapsed_seconds"] >= 60 and len(calls) == 2,
                        "sync pending",
                    )
                    require(
                        calls[-1]["action"] == "explicit_identical_redelivery"
                        and calls[-1]["http_status"] == 200,
                        "sync recovery",
                    )
        counts[(version, action, entry["classification"])] += 1
    return {
        "complete": True,
        "cases": 54,
        "matrix": [
            {"version": v, "action": a, "classification": c, "count": n}
            for (v, a, c), n in counts.items()
        ],
        "scope": (
            "Offline reconstruction from distributed copies; no scenario executed, "
            "no external replication claimed"
        ),
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parent), indent=2))

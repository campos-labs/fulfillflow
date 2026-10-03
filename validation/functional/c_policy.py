"""Closed result policy for independent evaluated cases; never promotes C4 to PASS."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validation.functional.c_observer import request_finished


def decision(case: Path, version: str, action: str, process: dict[str, Any]) -> str:
    def read(name: str) -> dict[str, Any]:
        data = json.loads((case / name).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("RESULT_OBJECT_REQUIRED")
        return data

    r = read("result.json")
    if (
        r.get("version") != version
        or r.get("action") != action
        or r.get("mode") != "evaluated"
        or r.get("cleanup_errors") != []
        or "error" in r
        or process.get("timed_out") is not False
        or process.get("tree_empty") is not True
        or process.get("forced_tree_cleanup") is not False
    ):
        raise ValueError("CASE_NOT_SAFE_TO_CONTINUE")
    children = r.get("children")
    cleanup = r.get("cleanup")
    if (
        not children
        or not cleanup
        or {(x["pid"], x["label"]) for x in children} != {(x["pid"], x["label"]) for x in cleanup}
        or any(x.get("exit_code") is None for x in cleanup)
    ):
        raise ValueError("CHILD_CLEANUP_UNPROVEN")
    if r.get("status") == "PASS" and process.get("exit_code") == 0:
        final = read("final.json")
        core = final["core"]
        tracking = final.get("tracking", core)
        if (
            len(tracking["inbox"]) != 1
            or tracking["inbox"][0]["status"] != "PROCESSED"
            or len(tracking["timeline"]) != 1
            or tracking["timeline"][0]["application_result"] != "APPLIED"
            or len(core["notifications"]) != 1
            or core["notifications"][0]["status"] != "SIMULATED"
            or len(core["shipments"]) != 1
            or core["shipments"][0]["status"] != "DELIVERED"
            or len(core["orders"]) != 1
            or core["orders"][0]["status"] != "FULFILLED"
        ):
            raise ValueError("FINAL_INVARIANTS_NOT_PROVEN")
        if (
            tracking["timeline"][0]["inbox_event_id"] != tracking["inbox"][0]["id"]
            or core["notifications"][0]["tracking_event_id"] != tracking["timeline"][0]["id"]
        ):
            raise ValueError("FINAL_IDENTITIES_DIVERGE")
        if version != "v1.0.0" and (
            len(core["receipts"]) != 1
            or core["receipts"][0]["event_id"] != tracking["timeline"][0]["id"]
        ):
            raise ValueError("FINAL_RECEIPT_DIVERGES")
        if version == "v1.2.0-rc.1" and any(
            len(final[owner][table]) != 1 or final[owner][table][0]["state"] != state
            for owner in ("core", "tracking")
            for table, state in (("message_inbox", "DONE"), ("message_outbox", "SENT"))
        ):
            raise ValueError("FINAL_MESSAGES_NOT_COMPLETE")
        return "PASS"
    if not (
        action == "unavailable"
        and r.get("status") == "INCONCLUSIVE"
        and process.get("exit_code") == 2
        and r.get("outcome") == "pending_at_limit_no_redelivery"
    ):
        raise ValueError("CASE_RESULT_BLOCKS_SEQUENCE")
    calls = r.get("webhook_calls", [])
    if len(calls) != 1 or calls[0].get("action") != "storage_unavailable":
        raise ValueError("UNEXPECTED_REDELIVERY")
    stopped = read("dependency-stopped.json")
    restored = read("dependency-restored.json")
    if (
        stopped.get("running") is not False
        or restored.get("running") is not True
        or stopped.get("container_id") != restored.get("container_id")
    ):
        raise ValueError("INTERVENTION_UNPROVEN")
    schedule = read("restoration-schedule.json")
    if (
        schedule.get("planned_seconds_from_offer") != 30
        or not 30 <= schedule["actual_seconds_from_offer"] <= 32
    ):
        raise ValueError("SCHEDULE_UNPROVEN")
    owners = {"core"} if version == "v1.0.0" else {"core", "tracking"}
    sql = read("authenticated-readiness.json")
    obs = read("request-completion.json")
    state = read("after-dependency-restore.json")
    if (
        set(sql) != owners
        or set(obs) != owners
        or set(state) != owners
        or any(x.get("select_one") != 1 for x in sql.values())
    ):
        raise ValueError("OBSERVATION_UNPROVEN")
    request = calls[0]["request_id"]
    public = [
        x
        for x in obs["core"].get("requests", [])
        if x["request_id"] == request and x["kind"] == "public_webhook"
    ]
    related = [
        x for state in obs.values() for x in state.get("requests", []) if x["request_id"] == request
    ]
    api_pids = {c["label"]: c["pid"] for c in children}
    if (
        len(public) != 1
        or not any(x.get("finished") is False for x in related)
        or any(obs[owner].get("pid") != api_pids.get(owner + "-api") for owner in owners)
    ):
        raise ValueError("PENDING_REQUEST_UNPROVEN")
    if len(public) != 1 or not all(
        x.get("pid") in {c["pid"] for c in children} for x in obs.values()
    ):
        raise ValueError("REQUEST_IDENTITY_UNPROVEN")
    core = state["core"]
    tracking = state.get("tracking", core)
    # Narrow qualified continuation: no durable admission/effects and request still pending.
    if (
        request_finished(obs, request)
        or tracking["inbox"]
        or tracking["timeline"]
        or core["notifications"]
        or core.get("receipts", [])
        or len(core["orders"]) != 1
        or len(core["shipments"]) != 1
        or core["orders"][0]["status"] != "CONFIRMED"
        or core["shipments"][0]["status"] != "PENDING"
    ):
        raise ValueError("PENDING_OUTCOME_NOT_QUALIFIED")
    if version == "v1.2.0-rc.1" and any(
        data[t] for data in state.values() for t in ("message_inbox", "message_outbox")
    ):
        raise ValueError("DURABLE_PENDING_NOT_QUALIFIED")
    return "INCONCLUSIVE"

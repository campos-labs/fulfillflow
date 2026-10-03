"""Owned PostgreSQL outage for C4; never selects resources by a broad name filter."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def check_owner(data: dict[str, Any], identifier: str, owner: str, port: str) -> None:
    bindings = data["HostConfig"]["PortBindings"].get("5432/tcp") or []
    if (
        not owner.startswith("ff-c-tcp-")
        or data["Id"] != identifier
        or data["Config"]["Labels"].get("org.fulfillflow.c.owner") != owner
        or not any(x["HostIp"] == "127.0.0.1" and x["HostPort"] in ("", port) for x in bindings)
    ):
        raise ValueError("DEPENDENCY_OWNERSHIP_MISMATCH")
    if data["State"]["Running"]:
        effective = data["NetworkSettings"]["Ports"].get("5432/tcp") or []
        if not any(x["HostIp"] == "127.0.0.1" and x["HostPort"] == port for x in effective):
            raise ValueError("DEPENDENCY_EFFECTIVE_PORT_MISMATCH")


def transition(identifier: str, owner: str, port: str, action: str) -> dict[str, Any]:
    if action not in {"stop", "start"}:
        raise ValueError("INVALID_DEPENDENCY_ACTION")

    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([DOCKER, *args], capture_output=True, timeout=35)

    def inspect() -> dict[str, Any]:
        result = run("inspect", identifier)
        if result.returncode:
            raise RuntimeError("DEPENDENCY_INSPECT_FAILED")
        data: dict[str, Any] = json.loads(result.stdout)[0]
        check_owner(data, identifier, owner, port)
        return data

    before = inspect()
    if action == "stop" and not before["State"]["Running"]:
        raise ValueError("DEPENDENCY_ALREADY_STOPPED")
    result = (
        run("stop", "--time", "10", identifier) if action == "stop" else run("start", identifier)
    )
    if result.returncode:
        raise RuntimeError("DEPENDENCY_TRANSITION_FAILED")
    if action == "start":
        deadline = time.monotonic() + 30
        while run("exec", identifier, "pg_isready", "-U", "postgres").returncode:
            if time.monotonic() >= deadline:
                raise TimeoutError("DEPENDENCY_NOT_READY")
            time.sleep(0.1)
    after_raw = run("inspect", identifier)
    if after_raw.returncode:
        raise RuntimeError("DEPENDENCY_INSPECT_FAILED")
    after = json.loads(after_raw.stdout)[0]
    check_owner(after, identifier, owner, port)
    if after["State"]["Running"] != (action == "start"):
        raise RuntimeError("DEPENDENCY_STATE_MISMATCH")
    return {
        "action": action,
        "container_id": identifier,
        "running": after["State"]["Running"],
        "exit_code": after["State"]["ExitCode"],
    }

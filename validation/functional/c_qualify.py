"""Owned development dependencies for six C3 qualification cases; stop on first failure."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from validation.functional.c_policy import decision
from validation.functional.c_process import run_owned
from validation.functional.c_runtime import save_record, verify_export
from validation.functional.environment import POSTGRES_IMAGE, RABBIT_IMAGE

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "validation/functional"
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def cleanup_succeeded(records: list[dict[str, Any]]) -> bool:
    return all(
        row.get("running") is False
        and row.get("exit_code") == 0
        and row.get("oom") is False
        and "error_type" not in row
        for row in records
    )


def main(
    output: Path,
    actions: tuple[str, ...] = ("control", "kill"),
    versions: tuple[str, ...] = ("v1.0.0", "v1.1.0-rc.1", "v1.2.0-rc.1"),
    *,
    evaluated: bool = False,
) -> None:
    if (
        not versions
        or len(set(versions)) != len(versions)
        or any(v not in {"v1.0.0", "v1.1.0-rc.1", "v1.2.0-rc.1"} for v in versions)
    ):
        raise ValueError("INVALID_VERSIONS")
    if not actions or any(
        x not in {"control", "kill", "healthy", "duplicate", "conflict", "unavailable"}
        for x in actions
    ):
        raise ValueError("INVALID_ACTIONS")
    if not output.resolve().is_relative_to(BASE):
        raise ValueError("OUTPUT_OUTSIDE_SCOPE")
    output.mkdir(exist_ok=False)
    package = output / "tool-package"
    package.mkdir()
    hashes = {}
    for source_file in sorted(BASE.glob("*.py")):
        archived_bytes = source_file.read_bytes()
        (package / source_file.name).write_bytes(archived_bytes)
        hashes[source_file.name] = hashlib.sha256(archived_bytes).hexdigest()
    save_record(output / "tool-hashes.json", hashes)
    runtimes = BASE / ".artifacts/c-runtimes-01"
    owner = "ff-c-tcp-" + secrets.token_hex(6)
    password = secrets.token_hex(24)
    env = os.environ.copy()
    env.update(
        POSTGRES_PASSWORD=password,
        RABBITMQ_DEFAULT_USER="cadmin",
        RABBITMQ_DEFAULT_PASS=password,
        RABBITMQ_CTL_ERL_ARGS="+S 1:1 +A 1",
    )
    containers: list[str] = []
    result: dict[str, Any] = {
        "mode": "evaluated" if evaluated else "development",
        "cases": [],
        "resources": [],
        "cleanup": [],
    }

    def command(*args: str) -> str:
        p = subprocess.run([DOCKER, *args], env=env, capture_output=True, timeout=40)
        if p.returncode:
            raise RuntimeError("DOCKER_COMMAND_FAILED_" + str(p.returncode))
        return p.stdout.decode().strip()

    def inspect(identifier: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(command("inspect", identifier))[0]
        if data["Config"]["Labels"].get("org.fulfillflow.c.owner") != owner:
            raise ValueError("OWNERSHIP_MISMATCH")
        return data

    try:
        result["concurrent_container_ids"] = command(
            "ps", "--no-trunc", "--format", "{{.ID}}"
        ).splitlines()
        ports = {}
        for role, image, target, mount in (
            ("postgres", POSTGRES_IMAGE, "5432", "/var/lib/postgresql"),
            ("rabbit", RABBIT_IMAGE, "5672", "/var/lib/rabbitmq"),
        ):
            image_info = json.loads(command("image", "inspect", image))[0]
            if not any(x.endswith("@" + image.split("@", 1)[1]) for x in image_info["RepoDigests"]):
                raise ValueError("IMAGE_IDENTITY_MISMATCH")
            volume = owner + "-" + role + "-data"
            command("volume", "create", "--label", "org.fulfillflow.c.owner=" + owner, volume)
            variables = (
                ["POSTGRES_PASSWORD"]
                if role == "postgres"
                else ["RABBITMQ_DEFAULT_USER", "RABBITMQ_DEFAULT_PASS", "RABBITMQ_CTL_ERL_ARGS"]
            )
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                chosen_port = str(port_socket.getsockname()[1])
            args = [
                "run",
                "-d",
                "--name",
                owner + "-" + role,
                "--hostname",
                owner + "-" + role,
                "--label",
                "org.fulfillflow.c.owner=" + owner,
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--mount",
                "type=volume,source=" + volume + ",target=" + mount,
                "-p",
                "127.0.0.1:" + chosen_port + ":" + target,
            ]
            for variable in variables:
                args.extend(["-e", variable])
            identifier = command(*args, image)
            containers.append(identifier)
            data = inspect(identifier)
            ports[role] = data["NetworkSettings"]["Ports"][target + "/tcp"][0]["HostPort"]
            result["resources"].append(
                {
                    "role": role,
                    "container_id": identifier,
                    "image_id": data["Image"],
                    "volume": volume,
                    "port": ports[role],
                }
            )
            deadline = time.monotonic() + 120
            ready_args = (
                ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
                if role == "postgres"
                else ["docker-entrypoint.sh", "rabbitmq-diagnostics", "-q", "check_running"]
            )
            while True:
                if not inspect(identifier)["State"]["Running"]:
                    raise RuntimeError("DEPENDENCY_EXITED_BEFORE_READY")
                ready = subprocess.run(
                    [DOCKER, "exec", identifier, *ready_args], capture_output=True, timeout=20
                )
                if ready.returncode == 0:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("DEPENDENCY_READINESS")
                time.sleep(0.5)
        for tag in versions:
            runtime = runtimes / tag
            verify_export(json.loads((runtime / "source.json").read_text(encoding="utf-8")))
            for action in actions:
                case = output / (tag + "-" + action)
                child = {
                    k: v
                    for k, v in os.environ.items()
                    if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "PATHEXT"}
                }
                child.update(
                    PYTHONPATH=os.pathsep.join((str(runtime / "source/src"), str(ROOT))),
                    PYTHONDONTWRITEBYTECODE="1",
                    PYTHONIOENCODING="utf-8",
                    C_PG_PORT=ports["postgres"],
                    C_PG_PASSWORD=password,
                    C_PG_CONTAINER=containers[0],
                    C_OWNER=owner,
                    C_EXECUTION_MODE="evaluated" if evaluated else "development",
                    TEMP=str(output),
                    TMP=str(output),
                )
                if tag == "v1.2.0-rc.1":
                    rabbit = containers[1]
                    vhost = owner + "-" + action
                    command(
                        "exec", rabbit, "docker-entrypoint.sh", "rabbitmqctl", "add_vhost", vhost
                    )
                    permissions = json.loads(
                        (runtime / "source/infrastructure/rabbitmq-v12.json").read_text()
                    )["permissions"]
                    for role in ("core", "tracking"):
                        user = action + "-" + role
                        pwd = secrets.token_hex(24)
                        command(
                            "exec",
                            rabbit,
                            "docker-entrypoint.sh",
                            "rabbitmqctl",
                            "add_user",
                            user,
                            pwd,
                        )
                        acl = next(row for row in permissions if row["user"] == role)
                        command(
                            "exec",
                            rabbit,
                            "docker-entrypoint.sh",
                            "rabbitmqctl",
                            "set_permissions",
                            "-p",
                            vhost,
                            user,
                            acl["configure"],
                            acl["write"],
                            acl["read"],
                        )
                        child["C_AMQP_" + role.upper()] = (
                            f"amqp://{user}:{pwd}@127.0.0.1:{ports['rabbit']}/{vhost}"
                        )
                started = time.monotonic()
                args = [
                    str(runtime / "venv/Scripts/python.exe"),
                    "-B",
                    "-m",
                    "validation.functional.c_tcp_case",
                    str(case),
                    str(runtime / "source.json"),
                    action,
                ]
                record: dict[str, Any] = {"version": tag, "action": action}
                if evaluated:
                    process = run_owned(args, env=child, cwd=runtime / "source", timeout=480)
                    record.update(process)
                    save_record(output / (tag + "-" + action + "-supervision.json"), process)
                    record["classification"] = decision(case, tag, action, process)
                else:
                    p = subprocess.run(
                        args, env=child, cwd=runtime / "source", capture_output=True, timeout=480
                    )
                    record.update(
                        exit_code=p.returncode,
                        seconds=time.monotonic() - started,
                        console_sha256=hashlib.sha256(p.stdout + p.stderr).hexdigest(),
                    )
                if (case / "result.json").exists():
                    record["result"] = json.loads((case / "result.json").read_text())
                result["cases"].append(record)
                save_record(output / (tag + "-" + action + "-process.json"), record)
                print(tag, action, record["exit_code"], flush=True)
                if record["exit_code"] and not evaluated:
                    raise RuntimeError("CASE_EXIT_NONZERO")
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__}
        raise
    finally:
        for identifier in reversed(containers):
            try:
                inspect(identifier)
                command("stop", "--time", "15", identifier)
                state = inspect(identifier)["State"]
                result["cleanup"].append(
                    {
                        "container_id": identifier,
                        "running": state["Running"],
                        "exit_code": state["ExitCode"],
                        "oom": state["OOMKilled"],
                    }
                )
            except Exception as exc:
                result["cleanup"].append(
                    {"container_id": identifier, "error_type": type(exc).__name__}
                )
        save_record(output / "summary.json", result)
        if "error" not in result and not cleanup_succeeded(result["cleanup"]):
            raise RuntimeError("CLEANUP_FAILED")


if __name__ == "__main__":
    main(Path(sys.argv[1]), tuple(sys.argv[2:]) or ("control", "kill"))

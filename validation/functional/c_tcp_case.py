"""Development C3 qualification: isolated TCP APIs, native process kill and SQL observations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import selectors
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from validation.functional.c_dependency import transition
from validation.functional.c_observer import request_finished
from validation.functional.c_runtime import save_record
from validation.functional.c_runtime_probe import capture
from validation.functional.support import controlled_error, hard_kill, read_marker

FOLDER = Path(__file__).resolve().parent


def require(ok: bool, code: str) -> None:
    if not ok:
        raise AssertionError(code)


def valid_marker(marker: dict[str, Any], tag: str, pid: int, token: str, event: str) -> bool:
    expected = "handler_sql_before_done_and_commit" if tag == "v1.2.0-rc.1" else "sql_before_commit"
    return (
        marker.get("marker") == expected
        and marker.get("pid") == pid
        and marker.get("token") == token
        and marker.get("event_id") == event
    )


def technical_complete(data: dict[str, Any]) -> bool:
    return all(
        len(data[owner][table]) == 1 and data[owner][table][0]["state"] == state
        for owner in ("core", "tracking")
        for table, state in (("message_inbox", "DONE"), ("message_outbox", "SENT"))
    )


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def environment(source: Path, output: Path, values: dict[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "PATHEXT"}
    }
    env.update(
        PYTHONPATH=os.pathsep.join((str(source / "src"), str(FOLDER), str(FOLDER.parents[1]))),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        PYTHONUNBUFFERED="1",
        FUNCTIONAL_APPLICATION_SOURCE=str(source),
        TEMP=str(output),
        TMP=str(output),
    )
    env.update(values)
    return env


def launch(
    args: list[str], source: Path, output: Path, values: dict[str, str]
) -> subprocess.Popen[bytes]:
    env = environment(source, output, values)
    executable = sys.executable
    if os.name == "nt":
        require(sys.version_info[:3] == (3, 13, 1), "UNQUALIFIED_NATIVE_INTERPRETER")
        native = getattr(sys, "_base_executable", None)
        if not isinstance(native, str) or not Path(native).is_file():
            raise ValueError("NATIVE_INTERPRETER_MISSING")
        executable = native
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return subprocess.Popen(
        [executable, "-B", *args],
        cwd=source,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def drain(child: subprocess.Popen[bytes]) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    assert child.stdout is not None
    while chunk := await asyncio.to_thread(child.stdout.read, 4096):
        digest.update(chunk)
        count += len(chunk)
    return {"bytes": count, "sha256": digest.hexdigest(), "raw_exported": False}


async def scenario(output: Path, source_record: Path, action: str) -> dict[str, Any]:
    require(
        action in {"control", "kill", "healthy", "duplicate", "conflict", "unavailable"},
        "INVALID_ACTION",
    )
    resolved = await asyncio.to_thread(output.resolve)
    require(resolved.is_relative_to(FOLDER), "OUTPUT_OUTSIDE_SCOPE")
    await asyncio.to_thread(output.mkdir, exist_ok=False)
    record = json.loads(await asyncio.to_thread(source_record.read_text, encoding="utf-8"))
    source = Path(record["source"])
    tag = record["tag"]
    result: dict[str, Any] = {
        "version": tag,
        "action": action,
        "mode": os.environ.get("C_EXECUTION_MODE", "development"),
        "status": "INVALID",
        "stage": "identity",
        "webhook_calls": [],
        "children": [],
        "cleanup_errors": [],
        "transport": "HTTP/TCP",
        "barrier_verified": False,
        "offered": False,
    }
    children: list[tuple[subprocess.Popen[bytes], asyncio.Task[dict[str, Any]], str]] = []
    writer = None
    server = None
    initial_request = None
    configs: dict[str, dict[str, str]] = {}
    key = hashlib.sha256(str(output).encode()).hexdigest()[:12]
    external = "c-" + key
    shipment_id = ""

    def spawn(args: list[str], cfg: dict[str, str], label: str) -> subprocess.Popen[bytes]:
        child = launch(args, source, output, cfg)
        children.append((child, asyncio.create_task(drain(child)), label))
        result["children"].append({"label": label, "pid": child.pid})
        return child

    async def snapshot() -> dict[str, Any]:
        data = {}
        for owner in ("core",) if tag == "v1.0.0" else ("core", "tracking"):
            conn = await psycopg.AsyncConnection.connect(
                configs[owner]["DATABASE_URL"].replace("postgresql+psycopg", "postgresql"),
                connect_timeout=3,
                row_factory=dict_row,
                options="-c statement_timeout=3000",
            )
            async with conn:
                owner_data: dict[str, Any] = {}
                if owner == "core":
                    for table, fields in (
                        ("shipments", "id,order_id,status"),
                        ("orders", "id,status"),
                        (
                            "notifications",
                            "id,tracking_event_id,shipment_id,status,created_at,simulated_at",
                        ),
                    ):
                        cur = await conn.execute(
                            sql.SQL("SELECT {} FROM {}").format(
                                sql.SQL(fields), sql.Identifier(table)
                            )
                        )
                        owner_data[table] = await cur.fetchall()
                    if tag != "v1.0.0":
                        cur = await conn.execute(
                            "SELECT event_id,content_sha256,result,created_at "
                            "FROM tracking_event_receipts"
                        )
                        owner_data["receipts"] = await cur.fetchall()
                if owner == "tracking" or tag == "v1.0.0":
                    cur = await conn.execute(
                        "SELECT id,status,payload_sha256,received_at,processed_at,request_id "
                        "FROM carrier_event_inbox WHERE external_event_id=%s",
                        (external,),
                    )
                    owner_data["inbox"] = await cur.fetchall()
                    cur = await conn.execute(
                        "SELECT id,inbox_event_id,application_result,created_at "
                        "FROM tracking_events"
                    )
                    owner_data["timeline"] = await cur.fetchall()
                if tag == "v1.2.0-rc.1":
                    for table in ("message_inbox", "message_outbox"):
                        cur = await conn.execute(
                            sql.SQL(
                                "SELECT message_id,event_id,state,attempts,generation FROM {}"
                            ).format(sql.Identifier(table))
                        )
                        owner_data[table] = await cur.fetchall()
                data[owner] = owner_data
        return cast(dict[str, Any], json.loads(json.dumps(data, default=str, sort_keys=True)))

    def pending(data: dict[str, Any]) -> bool:
        core = data["core"]
        tracking = data.get("tracking", core)
        return (
            len(tracking["inbox"]) == 1
            and tracking["inbox"][0]["status"] == "RECEIVED"
            and not tracking["timeline"]
            and not core["notifications"]
            and not core.get("receipts", [])
            and core["shipments"][0]["status"] == "PENDING"
            and core["orders"][0]["status"] == "CONFIRMED"
        )

    def complete(data: dict[str, Any]) -> bool:
        core = data["core"]
        tracking = data.get("tracking", core)
        return (
            len(tracking["inbox"]) == 1
            and tracking["inbox"][0]["status"] == "PROCESSED"
            and len(tracking["timeline"]) == len(core["notifications"]) == 1
            and core["notifications"][0]["status"] == "SIMULATED"
            and core["shipments"][0]["status"] == "DELIVERED"
            and core["orders"][0]["status"] == "FULFILLED"
            and (tag == "v1.0.0" or len(core["receipts"]) == 1)
            and (tag != "v1.2.0-rc.1" or technical_complete(data))
        )

    try:
        async with asyncio.timeout(420):
            capture(record)
            result["stage"] = "preparation"
            common = {
                "APP_ENV": "test",
                "C_REFERENCE": tag,
                "SESSION_SECRET": secrets.token_hex(24),
                "INTERNAL_API_SECRET": secrets.token_hex(24),
                "CARRIER_ALPHA_WEBHOOK_SECRET": secrets.token_hex(24),
                "CARRIER_BETA_WEBHOOK_SECRET": secrets.token_hex(24),
                "OTEL_ENABLED": "false",
                **({"C_OBSERVER_TOKEN": secrets.token_hex(24)} if action == "unavailable" else {}),
            }
            core_port, tracking_port = port(), port()
            require(core_port != tracking_port, "PORT_COLLISION")
            common.update(
                CORE_BASE_URL=f"http://127.0.0.1:{core_port}",
                TRACKING_BASE_URL=f"http://127.0.0.1:{tracking_port}",
            )
            admin = await psycopg.AsyncConnection.connect(
                host="127.0.0.1",
                port=int(os.environ["C_PG_PORT"]),
                user="postgres",
                password=os.environ["C_PG_PASSWORD"],
                dbname="postgres",
                autocommit=True,
            )
            async with admin:
                for owner in ("core",) if tag == "v1.0.0" else ("core", "tracking"):
                    name = f"c_{key}_{owner}"
                    pwd = secrets.token_hex(24)
                    await admin.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                            sql.Identifier(name), sql.Literal(pwd)
                        )
                    )
                    await admin.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {}").format(
                            sql.Identifier(name), sql.Identifier(name)
                        )
                    )
                    await admin.execute(
                        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                            sql.Identifier(name)
                        )
                    )
                    configs[owner] = dict(
                        common,
                        SERVICE_ROLE=owner,
                        C_PORT=str(core_port if owner == "core" else tracking_port),
                        DATABASE_URL=f"postgresql+psycopg://{name}:{pwd}@127.0.0.1:{os.environ['C_PG_PORT']}/{name}",
                        WORKER_HEARTBEAT_PATH=str(output / f"{owner}-heartbeat.json"),
                    )
                    if tag == "v1.2.0-rc.1":
                        # Fresh vhost per scenario, created by the outer resource owner.
                        configs[owner]["AMQP_URL"] = os.environ["C_AMQP_" + owner.upper()]
                    ini = "alembic.ini" if tag == "v1.0.0" else f"alembic_{owner}.ini"
                    child = spawn(
                        ["-m", "alembic", "-c", ini, "upgrade", "head"],
                        configs[owner],
                        "migration-" + owner,
                    )
                    require(await asyncio.to_thread(child.wait, 45) == 0, "MIGRATION_FAILED")
            connected = asyncio.get_running_loop().create_future()

            async def connect(
                reader: asyncio.StreamReader, conn_writer: asyncio.StreamWriter
            ) -> None:
                if connected.done():
                    conn_writer.close()
                else:
                    connected.set_result((reader, conn_writer))

            server = await asyncio.start_server(connect, "127.0.0.1", 0, limit=8192)
            token = secrets.token_hex(16)
            gate = {
                "C_GATE": "1",
                "TARGET_EXTERNAL_ID": external,
                "BARRIER_PORT": str(server.sockets[0].getsockname()[1]),
                "BARRIER_TOKEN": token,
            }
            core_cfg = (
                dict(configs["core"], **gate)
                if tag != "v1.2.0-rc.1" and action in {"control", "kill"}
                else configs["core"]
            )
            target = spawn([str(FOLDER / "c_api_child.py")], core_cfg, "core-api")
            if tag != "v1.0.0":
                spawn([str(FOLDER / "c_api_child.py")], configs["tracking"], "tracking-api")
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0), timeout=20, follow_redirects=False
            ) as client:

                async def ready(url: str) -> None:
                    async with asyncio.timeout(30):
                        while True:
                            try:
                                if (await client.get(url + "/health/ready")).status_code == 200:
                                    return
                            except httpx.TransportError:
                                pass
                            await asyncio.sleep(0.1)

                await ready(common["CORE_BASE_URL"])
                if tag != "v1.0.0":
                    await ready(common["TRACKING_BASE_URL"])
                base = common["CORE_BASE_URL"]
                response = await client.post(
                    base + "/api/v1/orders",
                    json={
                        "external_reference": external,
                        "recipient": {
                            "name": "Functional Recipient",
                            "email": "recipient@example.test",
                            "postal_code": "09700-000",
                            "city": "Sao Bernardo do Campo",
                            "state": "SP",
                        },
                    },
                )
                require(response.status_code == 201, "ORDER_CREATION")
                order = response.json()["id"]
                require(
                    (await client.post(base + f"/api/v1/orders/{order}/confirm")).status_code
                    == 200,
                    "ORDER_CONFIRM",
                )
                response = await client.post(
                    base + "/api/v1/shipments",
                    json={
                        "order_id": order,
                        "carrier_code": "carrier-alpha",
                        "tracking_code": external,
                    },
                )
                require(response.status_code == 201, "SHIPMENT_CREATION")
                shipment_id = response.json()["id"]
                body = json.dumps(
                    {
                        "eventId": external,
                        "trackingCode": external,
                        "status": "DELIVERED",
                        "eventDate": "2026-08-29T11:30:00Z",
                        "city": "Sao Bernardo do Campo",
                        "description": "C development qualification",
                    },
                    sort_keys=True,
                ).encode()
                from fulfillflow.tracking.public import calculate_signature

                async def post_event(label: str, payload: bytes = body) -> dict[str, Any]:
                    timestamp = str(int(time.time()))
                    signature = calculate_signature(
                        common["CARRIER_ALPHA_WEBHOOK_SECRET"],
                        timestamp=timestamp,
                        event_id=external,
                        raw_body=payload,
                    )
                    call = {
                        "action": label,
                        "request_id": str(uuid5(NAMESPACE_URL, external + label)),
                        "number": len(result["webhook_calls"]) + 1,
                        "raw_sha256": hashlib.sha256(payload).hexdigest(),
                    }
                    result["webhook_calls"].append(call)
                    result["offered"] = True
                    try:
                        response = await client.post(
                            base + "/api/v1/carriers/carrier-alpha/events",
                            content=payload,
                            headers={
                                "Content-Type": "application/json",
                                "X-Request-ID": str(call["request_id"]),
                                "X-FulfillFlow-Event-Id": external,
                                "X-FulfillFlow-Timestamp": timestamp,
                                "X-FulfillFlow-Signature": signature,
                            },
                        )
                        call["http_status"] = response.status_code
                        return cast(dict[str, Any], response.json())
                    except httpx.TransportError as exc:
                        call["transport_error"] = type(exc).__name__
                        return {}

                if action == "unavailable":
                    if tag == "v1.2.0-rc.1":
                        for owner in ("core", "tracking"):
                            worker = spawn(
                                ["-m", f"fulfillflow.{owner}.worker"],
                                configs[owner],
                                owner + "-worker",
                            )
                            async with asyncio.timeout(30):
                                while True:
                                    heartbeat = output / f"{owner}-heartbeat.json"
                                    if heartbeat.exists():
                                        state = json.loads(heartbeat.read_text())
                                        if state["pid"] == worker.pid and all(
                                            x["state"] == "ready" for x in state["stages"].values()
                                        ):
                                            break
                                    await asyncio.sleep(0.1)
                    result["stage"] = "dependency_outage"
                    dependency_args = (
                        os.environ["C_PG_CONTAINER"],
                        os.environ["C_OWNER"],
                        os.environ["C_PG_PORT"],
                    )
                    save_record(
                        output / "dependency-stopped.json",
                        await asyncio.to_thread(transition, *dependency_args, "stop"),
                    )
                    offered_at = time.monotonic()
                    await post_event("storage_unavailable")
                    await asyncio.sleep(max(0, offered_at + 30 - time.monotonic()))
                    restoration_started = time.monotonic()
                    save_record(
                        output / "restoration-schedule.json",
                        {
                            "planned_seconds_from_offer": 30,
                            "actual_seconds_from_offer": restoration_started - offered_at,
                        },
                    )
                    if restoration_started - offered_at > 32:
                        raise RuntimeError("RESTORATION_SCHEDULE_MISSED")
                    save_record(
                        output / "dependency-restored.json",
                        await asyncio.to_thread(transition, *dependency_args, "start"),
                    )
                    authenticated = {}
                    for owner, config in configs.items():
                        conn = await psycopg.AsyncConnection.connect(
                            config["DATABASE_URL"].replace("postgresql+psycopg", "postgresql"),
                            connect_timeout=3,
                        )
                        async with conn:
                            cursor = await conn.execute(
                                "SELECT current_database(), current_user, 1"
                            )
                            row = await cursor.fetchone()
                            assert row is not None and row[2] == 1, "SQL_RESTORATION_NOT_READY"
                            authenticated[owner] = {
                                "database": row[0],
                                "role": row[1],
                                "select_one": row[2],
                            }
                    save_record(output / "authenticated-readiness.json", authenticated)
                    request_id = result["webhook_calls"][0]["request_id"]
                    deadline = time.monotonic() + 60
                    observer_states = {}
                    while True:
                        for owner in configs:
                            url = common[
                                "CORE_BASE_URL" if owner == "core" else "TRACKING_BASE_URL"
                            ]
                            response = await client.get(
                                url + "/__c_observer",
                                headers={"X-C-Observer": common["C_OBSERVER_TOKEN"]},
                            )
                            require(response.status_code == 200, "OBSERVER_UNAVAILABLE")
                            observer_states[owner] = response.json()
                        restored_state = await snapshot()
                        finished = request_finished(observer_states, request_id)
                        business = restored_state.get("tracking", restored_state["core"])
                        empty = (
                            not business["inbox"]
                            and not business["timeline"]
                            and not restored_state["core"]["notifications"]
                            and not restored_state["core"].get("receipts", [])
                            and restored_state["core"]["shipments"][0]["status"] == "PENDING"
                            and restored_state["core"]["orders"][0]["status"] == "CONFIRMED"
                        )
                        if tag == "v1.2.0-rc.1":
                            empty = empty and all(
                                not owned[t]
                                for owned in restored_state.values()
                                for t in ("message_inbox", "message_outbox")
                            )
                        if finished and (complete(restored_state) or empty):
                            break
                        if time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(0.1)
                    save_record(output / "request-completion.json", observer_states)
                    save_record(output / "after-dependency-restore.json", restored_state)
                    if finished and complete(restored_state):
                        result["outcome"] = "completed_without_redelivery"
                    elif finished and empty:
                        result["outcome"] = "completed_after_explicit_redelivery"
                        await post_event("explicit_delivery_after_storage_restore")
                        require(
                            result["webhook_calls"][-1].get("http_status")
                            == (202 if tag == "v1.2.0-rc.1" else 200),
                            "RESTORED_DELIVERY_FAILED",
                        )
                        async with asyncio.timeout(60):
                            while True:
                                restored_state = await snapshot()
                                if complete(restored_state):
                                    break
                                await asyncio.sleep(0.1)
                    else:
                        result["outcome"] = "pending_at_limit_no_redelivery"
                        result["status"] = "INCONCLUSIVE"
                        return result
                    save_record(output / "final.json", restored_state)
                    require(
                        len(result["webhook_calls"]) == (2 if empty else 1),
                        "UNEXPECTED_WEBHOOK_COUNT",
                    )
                    result["status"] = "PASS"
                    return result
                if tag == "v1.2.0-rc.1":
                    await post_event("initial")
                    require(
                        result["webhook_calls"][-1].get("http_status") == 202, "ADMISSION_FAILED"
                    )
                    conn = await psycopg.AsyncConnection.connect(
                        configs["tracking"]["DATABASE_URL"].replace(
                            "postgresql+psycopg", "postgresql"
                        )
                    )
                    async with conn:
                        cursor = await conn.execute(
                            "SELECT command FROM carrier_event_inbox WHERE external_event_id=%s",
                            (external,),
                        )
                        row = await cursor.fetchone()
                        assert row is not None, "COMMAND_MISSING"
                        event_id = row[0]["event_id"]
                    spawn(
                        ["-m", "fulfillflow.tracking.worker"],
                        configs["tracking"],
                        "tracking-worker",
                    )
                    if action in {"control", "kill"}:
                        target = spawn(
                            [str(FOLDER / "probe.py")],
                            dict(
                                configs["core"],
                                **gate,
                                TARGET_EVENT_ID=event_id,
                                TARGET_SHIPMENT_ID=shipment_id,
                            ),
                            "core-gated-worker",
                        )
                    else:
                        spawn(["-m", "fulfillflow.core.worker"], configs["core"], "core-worker")
                else:
                    event_id = external
                    initial_request = asyncio.create_task(post_event("initial"))
                if action in {"control", "kill"}:
                    result["stage"] = "barrier"
                    reader, writer = await asyncio.wait_for(connected, 30)
                    marker = await read_marker(reader, target, 30)
                    require(
                        valid_marker(marker, tag, target.pid, token, event_id),
                        "BARRIER_IDENTITY_MISMATCH",
                    )
                    require(target.poll() is None, "CHILD_NOT_ALIVE")
                    marker.pop("token", None)
                    save_record(output / "barrier.json", marker)
                    outside = await snapshot()
                    require(pending(outside), "EARLY_EFFECTS")
                    save_record(output / "outside-barrier.json", outside)
                    result["barrier_verified"] = True
                    if action == "kill":
                        result["stage"] = "kill"
                        result["kill"] = await hard_kill(target)
                        require(result["kill"]["killed"] is True, "NO_KILL")
                        after = await snapshot()
                        require(after == outside, "POST_KILL_STATE_CHANGED")
                        save_record(output / "after-kill.json", after)
                        if initial_request:
                            await initial_request
                        result["stage"] = "restart"
                        if tag == "v1.2.0-rc.1":
                            spawn(
                                ["-m", "fulfillflow.core.worker"],
                                configs["core"],
                                "core-restarted-worker",
                            )
                        else:
                            spawn(
                                [str(FOLDER / "c_api_child.py")],
                                configs["core"],
                                "core-restarted-api",
                            )
                            await ready(base)
                        result["recovery_action"] = "explicit_process_restart"
                        result["stage"] = "observation_without_redelivery"
                        observation_started = time.monotonic()
                        deadline = observation_started + 60
                        observations = 0
                        while True:
                            observed = await snapshot()
                            observations += 1
                            if complete(observed):
                                break
                            if tag != "v1.2.0-rc.1":
                                require(pending(observed), "UNEXPECTED_STATE_DURING_WAIT")
                            if time.monotonic() >= deadline:
                                break
                            await asyncio.sleep(0.1)
                        save_record(
                            output / "without-redelivery.json",
                            {
                                "snapshot": observed,
                                "observations": observations,
                                "complete": complete(observed),
                                "window_seconds": 60,
                                "elapsed_seconds": time.monotonic() - observation_started,
                            },
                        )
                        if tag != "v1.2.0-rc.1":
                            require(not complete(observed), "UNEXPECTED_AUTONOMOUS_COMPLETION")
                            await post_event("explicit_identical_redelivery")
                            require(
                                result["webhook_calls"][-1].get("http_status") == 200,
                                "REDELIVERY_FAILED",
                            )
                        else:
                            require(complete(observed), "ASYNC_RECOVERY_NOT_COMPLETED")
                    else:
                        writer.write(b"release\n")
                        await writer.drain()
                        if initial_request:
                            await initial_request
                elif initial_request:
                    await initial_request
                if action != "kill":
                    require(
                        result["webhook_calls"][-1].get("http_status")
                        == (202 if tag == "v1.2.0-rc.1" else 200),
                        "CONTROL_HTTP_FAILED",
                    )
                result["stage"] = "completion"
                async with asyncio.timeout(60):
                    while True:
                        final = await snapshot()
                        if complete(final):
                            break
                        await asyncio.sleep(0.1)
                save_record(output / "initial-complete.json", final)
                if action in {"duplicate", "conflict"}:
                    result["stage"] = "terminal_reoffer"
                    changed = dict(
                        json.loads(body), description="Changed authenticated description"
                    )
                    payload = (
                        body
                        if action == "duplicate"
                        else json.dumps(changed, sort_keys=True).encode()
                    )
                    reply = await post_event(action, payload)
                    status = result["webhook_calls"][-1].get("http_status")
                    if action == "conflict":
                        require(
                            status == 409 and reply.get("code") == "EVENT_ID_PAYLOAD_CONFLICT",
                            "CONFLICT_RESPONSE",
                        )
                    else:
                        require(
                            status == 200 and reply.get("result") == "DUPLICATE",
                            "DUPLICATE_RESPONSE",
                        )
                    after = await snapshot()
                    save_record(output / "after-reoffer.json", after)
                    require(after == final, "TERMINAL_STATE_CHANGED")
                save_record(output / "final.json", final)
                require(
                    len(result["webhook_calls"])
                    == (
                        2
                        if action in {"duplicate", "conflict", "unavailable"}
                        or (action == "kill" and tag != "v1.2.0-rc.1")
                        else 1
                    ),
                    "UNEXPECTED_WEBHOOK_COUNT",
                )
                result["status"] = "PASS"
    except Exception as exc:
        result["status"] = (
            ("FAIL" if isinstance(exc, AssertionError) else "INCONCLUSIVE")
            if result["offered"]
            else "INVALID"
        )
        result["error"] = controlled_error(exc, result["stage"])
        if isinstance(exc, AssertionError) and len(exc.args) == 1:
            result["assertion"] = exc.args[0]
    finally:
        if initial_request and not initial_request.done():
            initial_request.cancel()
            await asyncio.gather(initial_request, return_exceptions=True)
        if writer:
            writer.close()
        if server:
            server.close()
            await server.wait_closed()
        for child, task, label in reversed(children):
            try:
                exit_result = await hard_kill(child)
                captured = await asyncio.wait_for(task, 5)
                result.setdefault("cleanup", []).append(
                    {"label": label, **exit_result, "output": captured}
                )
            except Exception as exc:
                result["cleanup_errors"].append(controlled_error(exc, "shutdown"))
        if result["cleanup_errors"]:
            result["status"] = "INCONCLUSIVE"
        save_record(output / "result.json", result)
    return result


if __name__ == "__main__":
    with asyncio.Runner(
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    ) as runner:
        result = runner.run(scenario(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
    print(json.dumps({"status": result["status"], "stage": result["stage"]}))
    raise SystemExit(0 if result["status"] == "PASS" else 2)

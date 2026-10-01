"""One isolated B scenario: real databases/broker and real owner worker processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import aio_pika
import httpx
import psycopg
from environment import ConnectionInfo, Infrastructure
from psycopg import sql
from snapshot import check_complete, check_pending, complete, require, snapshot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.config import Settings
from fulfillflow.contracts.messages import MessageEnvelope
from fulfillflow.core.message_handler import apply_command
from fulfillflow.core.message_tables import tables as core_tables
from fulfillflow.db import Database
from fulfillflow.main import create_app as create_core
from fulfillflow.messaging.amqp import declare_flow, publish_batch, receive
from fulfillflow.messaging.store import process_one
from fulfillflow.shared import SystemClock
from fulfillflow.tracking.app import create_app as create_tracking
from fulfillflow.tracking.message_tables import tables as tracking_tables
from fulfillflow.tracking.public import calculate_signature
from support import controlled_error, hard_kill, read_marker, write_json

FOLDER = Path(__file__).resolve().parent


class ScenarioExportError(RuntimeError):
    """Keep the already-sanitized primary result available to the coordinator."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__("SCENARIO_EXPORT_FAILED")


def environment(source: Path, cwd: Path, values: dict[str, str]) -> dict[str, str]:
    retained = {"SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "PATHEXT"}
    result: dict[str, Any] = {
        key: value for key, value in os.environ.items() if key.upper() in retained
    }
    result.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join((str(source / "src"), str(FOLDER))),
        PYTHONIOENCODING="utf-8",
        PYTHONUNBUFFERED="1",
        FUNCTIONAL_APPLICATION_SOURCE=str(source),
        TEMP=str(cwd),
        TMP=str(cwd),
    )
    result.update(values)
    return result


def launch(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    executable = sys.executable
    child_env = dict(env)
    if os.name == "nt" and sys.prefix != sys.base_prefix:
        # CPython's Windows venv executable is a redirector process. Launch the
        # real interpreter with the same venv identity so Popen owns the worker PID.
        if sys.version_info[:3] != (3, 13, 1):
            raise ValueError("WINDOWS_VENV_RUNTIME_NOT_VERIFIED")
        base_executable = getattr(sys, "_base_executable", None)
        if not isinstance(base_executable, str) or not Path(base_executable).is_file():
            raise ValueError("NATIVE_PYTHON_EXECUTABLE_MISSING")
        executable = base_executable
        child_env["__PYVENV_LAUNCHER__"] = sys.executable
    return subprocess.Popen(
        [executable, "-B", *args],
        cwd=cwd,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def launch_worker(
    args: list[str], source: Path, output: Path, values: dict[str, str]
) -> subprocess.Popen[bytes]:
    # Frozen worker schema checks resolve Alembic from cwd. Reject dotenv rather
    # than silently importing settings from the source checkout into this fixture.
    if (source / ".env").exists():
        raise ValueError("APPLICATION_DOTENV_NOT_ALLOWED")
    return launch(args, source, environment(source, output, values))


async def capture_output(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    # Drain continuously to avoid blocking the child. Export only classified counters.
    digest = hashlib.sha256()
    count = 0
    errors = 0
    diagnostics = []
    assert process.stdout is not None
    while True:
        chunk = await asyncio.to_thread(process.stdout.readline)
        if not chunk:
            break
        count += 1
        digest.update(chunk)
        if b"ERROR" in chunk or b"Traceback" in chunk:
            errors += 1
        if len(chunk) <= 8192:
            try:
                value = json.loads(chunk)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict) and value.get("marker") in ("probe_error", "probe_exit"):
                error = value.get("error", {})
                if isinstance(error, dict):
                    safe = {}
                    for key in ("stage", "exception_type", "code"):
                        item = error.get(key)
                        if (
                            isinstance(item, str)
                            and len(item) <= 80
                            and all(character.isalnum() or character == "_" for character in item)
                        ):
                            safe[key] = item
                    for key in ("errno", "winerror"):
                        if type(error.get(key)) is int:
                            safe[key] = error[key]
                    diagnostics.append(
                        {
                            "marker": value["marker"],
                            "error": safe,
                            "origin": value.get("origin")
                            if value.get("origin") in ("application", "instrumentation")
                            else None,
                        }
                    )

    return {
        "lines": count,
        "sha256_raw_stream": digest.hexdigest(),
        "error_lines": errors,
        "raw_stream_exported": False,
        "diagnostics": diagnostics,
    }


async def setup_databases(
    infra: Infrastructure,
    info: ConnectionInfo,
    source: Path,
    output: Path,
    key: str,
    progress: dict[str, Any],
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    common = {
        "APP_ENV": "test",
        "INTERNAL_API_SECRET": secrets.token_hex(32),
        "SESSION_SECRET": secrets.token_hex(32),
        "CARRIER_ALPHA_WEBHOOK_SECRET": secrets.token_hex(32),
        "CARRIER_BETA_WEBHOOK_SECRET": secrets.token_hex(32),
        "DB_POOL_SIZE": "3",
        "DB_MAX_OVERFLOW": "0",
        "DB_POOL_TIMEOUT_SECONDS": "5",
        "DB_STATEMENT_TIMEOUT_MS": "5000",
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "METRICS_ENABLED": "true",
        "OTEL_ENABLED": "false",
    }
    configs = {}
    progress["preparation_step"] = "postgres_connect"
    admin = await psycopg.AsyncConnection.connect(
        host="127.0.0.1",
        port=info.postgres_port,
        user=info.postgres_user,
        password=info.postgres_password,
        dbname="postgres",
        autocommit=True,
    )
    progress["preparation_step"] = "postgres_create_databases"
    async with admin:
        for owner in ("core", "tracking"):
            name = f"fb_{key}_{owner}"
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
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
            )
            configs[owner] = dict(
                common,
                SERVICE_ROLE=owner,
                DATABASE_URL=f"postgresql+psycopg://{name}:{pwd}@127.0.0.1:{info.postgres_port}/{name}",
            )
    progress["preparation_step"] = "rabbit_create_vhost"
    vhost = f"fb-{key}"
    await infra.exec_owned("rabbit", ["rabbitmqctl", "add_vhost", vhost])
    await infra.exec_owned(
        "rabbit",
        ["rabbitmqctl", "set_permissions", "-p", vhost, info.rabbit_user, ".*", ".*", ".*"],
    )
    progress["preparation_step"] = "rabbit_create_roles"
    permissions = json.loads((source / "infrastructure/rabbitmq-v12.json").read_text())[
        "permissions"
    ]
    for owner in ("core", "tracking"):
        user = f"fb_{key}_{owner}"
        password = secrets.token_hex(24)
        await infra.exec_owned("rabbit", ["rabbitmqctl", "add_user", user, password])
        permission = next(row for row in permissions if row["user"] == owner)
        await infra.exec_owned(
            "rabbit",
            [
                "rabbitmqctl",
                "set_permissions",
                "-p",
                vhost,
                user,
                permission["configure"],
                permission["write"],
                permission["read"],
            ],
        )
        configs[owner]["AMQP_URL"] = f"amqp://{user}:{password}@127.0.0.1:{info.amqp_port}/{vhost}"
    amqp_url = (
        f"amqp://{info.rabbit_user}:{info.rabbit_password}@127.0.0.1:{info.amqp_port}/{vhost}"
    )
    progress["preparation_step"] = "rabbit_connect_admin"
    admin_connection = await aio_pika.connect(amqp_url, timeout=5)
    async with admin_connection:
        progress["preparation_step"] = "rabbit_declare_flows"
        admin_channel = await admin_connection.channel()
        for flow in ("tracking.apply.v1", "tracking.result.v1"):
            await declare_flow(admin_channel, flow)
    migration_results = {}
    for owner in ("core", "tracking"):
        progress["preparation_step"] = f"migrate_{owner}"
        configs[owner]["WORKER_HEARTBEAT_PATH"] = str(output / f"{owner}-worker-heartbeat.json")
        process = launch(
            ["-m", "alembic", "-c", str(source / f"alembic_{owner}.ini"), "upgrade", "head"],
            output,
            environment(source, output, configs[owner]),
        )
        draining = asyncio.create_task(capture_output(process))
        try:
            code = await asyncio.to_thread(process.wait, 45)
        except BaseException:
            await hard_kill(process)
            await draining
            raise
        migration_results[owner] = dict(await draining, exit_code=code)
        write_json(output / f"migration-{owner}.json", migration_results[owner])
        require(code == 0, "MIGRATION_FAILED")
    write_json(output / "migrations.json", migration_results)
    return configs, {
        "vhost": vhost,
        "amqp_acl": permissions,
        "database_names": {owner: f"fb_{key}_{owner}" for owner in ("core", "tracking")},
    }


def settings(values: dict[str, str]) -> Settings:
    # Explicit values override external application settings; all other fields use frozen defaults.
    defaults = {
        name: field.default
        for name, field in Settings.model_fields.items()
        if not field.is_required()
    }
    defaults.update(
        {
            key.lower(): value
            for key, value in values.items()
            if key.lower() in Settings.model_fields
        }
    )
    return Settings(_env_file=None, **defaults)


async def scenario(
    source: Path,
    infra: Infrastructure,
    info: ConnectionInfo,
    output: Path,
    owner: str,
    action: str,
    repetition: int,
) -> dict[str, Any]:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    begun = time.monotonic()
    result: dict[str, Any] = {
        "owner": owner,
        "action": action,
        "repetition": repetition,
        "started_at": datetime.now(UTC).isoformat(),
        "stage": "preparation",
        "status": "INVALID",
        "intervention_confirmed": False,
        "webhook_requests": 0,
        "errors": [],
        "cleanup_errors": [],
        "export_errors": [],
        "processes": [],
        "transport": {
            "admission": "ASGI",
            "service_queries": "ASGI",
            "messages": "RabbitMQ TCP",
            "workers": "native OS processes",
        },
        "observer_limits": {
            "prepare": 120,
            "barrier": 30,
            "kill": 15,
            "rollback": 30,
            "recovery": 60,
            "global": 300,
        },
    }
    children: list[tuple[subprocess.Popen[bytes], asyncio.Task[dict[str, Any]]]] = []
    server = None
    writer = None
    core = tracking = None
    key = hashlib.sha256(str(output.relative_to(FOLDER)).encode()).hexdigest()[:12]

    async def spawn(args: list[str], cfg: dict[str, str], label: str) -> subprocess.Popen[bytes]:
        isolated = dict(cfg, WORKER_HEARTBEAT_PATH=str(output / f"{label}-heartbeat.json"))
        process = launch_worker(args, source, output, isolated)
        task = asyncio.create_task(capture_output(process))
        children.append((process, task))
        result["processes"].append({"label": label, "pid": process.pid})
        return process

    async def queue_state(vhost: str) -> dict[str, Any]:
        raw = await infra.exec_owned(
            "rabbit",
            [
                "rabbitmqctl",
                "list_queues",
                "-p",
                vhost,
                "name",
                "messages_ready",
                "messages_unacknowledged",
                "--formatter",
                "json",
            ],
        )
        data = json.loads(raw)
        target = f"tracking.{'apply' if owner == 'core' else 'result'}.v1.queue"
        relevant = [row for row in data if row["name"] == target]
        require(len(relevant) == 1, "QUEUE_OBSERVATION_MISSING")
        require(int(relevant[0]["messages_ready"]) == 0, "QUEUE_NOT_EMPTY")
        require(int(relevant[0]["messages_unacknowledged"]) == 0, "ACK_NOT_CONFIRMED")
        return dict(relevant[0])

    try:
        async with asyncio.timeout(270):
            async with asyncio.timeout_at(begun + 120):
                configs, names = await setup_databases(infra, info, source, output, key, result)
                result["resources"] = names
                cs, ts = settings(configs["core"]), settings(configs["tracking"])
                core, tracking = Database.from_settings(cs), Database.from_settings(ts)
                core_app = create_core(cs, core, source / "alembic_core.ini")
                tracking_app = create_tracking(
                    ts,
                    tracking,
                    source / "alembic_tracking.ini",
                    service_transport=httpx.ASGITransport(app=core_app, raise_app_exceptions=False),
                )
                core_app.state.service_transport = httpx.ASGITransport(
                    app=tracking_app, raise_app_exceptions=False
                )
            async with AsyncExitStack() as stack:
                preparation_tail = await stack.enter_async_context(asyncio.timeout_at(begun + 120))
                await stack.enter_async_context(core_app.router.lifespan_context(core_app))
                await stack.enter_async_context(tracking_app.router.lifespan_context(tracking_app))
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=core_app), base_url="http://functional"
                    )
                )
                clock = SystemClock()
                # No supplier secrets or payload echoes in exported records.
                external_id = f"b-{key}"
                order_resp = await client.post(
                    "/api/v1/orders",
                    json={
                        "external_reference": external_id,
                        "recipient": {
                            "name": "Functional Recipient",
                            "email": "recipient@example.test",
                            "postal_code": "09700-000",
                            "city": "Sao Bernardo do Campo",
                            "state": "SP",
                        },
                    },
                )
                require(order_resp.status_code == 201, "ORDER_CREATION")
                order_id = order_resp.json()["id"]
                require(
                    (await client.post(f"/api/v1/orders/{order_id}/confirm")).status_code == 200,
                    "ORDER_CONFIRM",
                )
                shipment_resp = await client.post(
                    "/api/v1/shipments",
                    json={
                        "order_id": order_id,
                        "carrier_code": "carrier-alpha",
                        "tracking_code": external_id,
                    },
                )
                require(shipment_resp.status_code == 201, "SHIPMENT_CREATION")
                shipment_id = shipment_resp.json()["id"]
                body = json.dumps(
                    {
                        "eventId": external_id,
                        "trackingCode": external_id,
                        "status": "DELIVERED",
                        "eventDate": "2026-08-29T11:30:00Z",
                        "city": "Sao Bernardo do Campo",
                        "description": "Functional verification",
                    },
                    sort_keys=True,
                ).encode()
                request_id = str(uuid5(NAMESPACE_URL, external_id))

                async def post_event() -> httpx.Response:
                    timestamp = str(int(clock.now().timestamp()))
                    signature = calculate_signature(
                        configs["tracking"]["CARRIER_ALPHA_WEBHOOK_SECRET"],
                        timestamp=timestamp,
                        event_id=external_id,
                        raw_body=body,
                    )
                    result["webhook_requests"] += 1
                    return await client.post(
                        "/api/v1/carriers/carrier-alpha/events",
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-FulfillFlow-Event-Id": external_id,
                            "X-FulfillFlow-Timestamp": timestamp,
                            "X-FulfillFlow-Signature": signature,
                            "X-Request-ID": request_id,
                        },
                    )

                accepted = await post_event()
                require(accepted.status_code == 202, "DURABLE_ADMISSION")
                inbox_id = accepted.json()["inbox_event_id"]
                async with tracking.session() as session:
                    command = await session.scalar(
                        text("SELECT command FROM carrier_event_inbox WHERE id=:id"),
                        {"id": inbox_id},
                    )
                event_id = command["event_id"]
                result["identities"] = {
                    "event_id": event_id,
                    "inbox_id": inbox_id,
                    "shipment_id": shipment_id,
                    "order_id": order_id,
                    "request_id": request_id,
                    "raw_sha256": hashlib.sha256(body).hexdigest(),
                }
                core_connection = await aio_pika.connect(configs["core"]["AMQP_URL"], timeout=5)
                async with core_connection:
                    tracking_connection = await stack.enter_async_context(
                        await aio_pika.connect(configs["tracking"]["AMQP_URL"], timeout=5)
                    )
                    channel = await core_connection.channel(
                        publisher_confirms=True, on_return_raises=True
                    )
                    tracking_channel = await tracking_connection.channel(
                        publisher_confirms=True, on_return_raises=True
                    )
                    await publish_batch(tracking, tracking_tables, tracking_channel, clock)
                    queue = await channel.get_queue("tracking.apply.v1.queue")
                    await receive(
                        core,
                        core_tables,
                        await queue.get(timeout=5),
                        "tracking.apply.v1",
                        clock.now(),
                    )
                    if owner == "tracking":

                        async def apply(session: AsyncSession, envelope: MessageEnvelope) -> None:
                            await apply_command(session, envelope, clock)

                        async with core.session() as session, session.begin():
                            require(
                                await process_one(session, core_tables.inbox, clock.now(), apply),
                                "CORE_PREPARATION",
                            )
                        await publish_batch(core, core_tables, channel, clock)
                        queue = await tracking_channel.get_queue("tracking.result.v1.queue")
                        await receive(
                            tracking,
                            tracking_tables,
                            await queue.get(timeout=5),
                            "tracking.result.v1",
                            clock.now(),
                        )
                result["queue_before_worker"] = await queue_state(names["vhost"])
                initial = await snapshot(core, tracking, event_id, inbox_id, shipment_id)
                check_pending(initial, owner)
                write_json(output / "before.json", initial)
                preparation_tail.reschedule(None)
                result["stage"] = "barrier"
                connected = asyncio.get_running_loop().create_future()

                async def connect(
                    reader: asyncio.StreamReader, connection_writer: asyncio.StreamWriter
                ) -> None:
                    if connected.done():
                        connection_writer.close()
                        await connection_writer.wait_closed()
                    else:
                        connected.set_result((reader, connection_writer))

                server = await asyncio.start_server(connect, "127.0.0.1", 0, limit=8192)
                token = secrets.token_hex(16)
                gated_env = dict(
                    configs[owner],
                    BARRIER_PORT=str(server.sockets[0].getsockname()[1]),
                    BARRIER_TOKEN=token,
                    TARGET_EVENT_ID=event_id,
                    TARGET_SHIPMENT_ID=shipment_id,
                )
                async with asyncio.timeout(30):
                    child = await spawn([str(FOLDER / "probe.py")], gated_env, "instrumented")
                    reader, writer = await connected
                    marker = await read_marker(reader, child, 30)
                    result["barrier_identity"] = {
                        "pid_expected": child.pid,
                        "pid_observed": marker.get("pid")
                        if type(marker.get("pid")) is int
                        else None,
                        "token_matches": marker.get("token") == token,
                        "event_matches": marker.get("event_id") == event_id,
                        "owner_matches": marker.get("owner") == owner,
                    }
                    require(
                        marker.get("token") == token
                        and marker.get("pid") == child.pid
                        and marker.get("event_id") == event_id
                        and marker.get("owner") == owner,
                        "BARRIER_IDENTITY_MISMATCH",
                    )
                    if marker.get("marker") == "probe_error":
                        marker.pop("token", None)
                        write_json(output / "probe-error.json", marker)
                        result["probe_error"] = marker
                        raise RuntimeError("PROBE_REPORTED_FAILURE")
                    require(
                        marker.get("event_id") == event_id
                        and marker.get("owner") == owner
                        and marker.get("marker") == "handler_sql_before_done_and_commit",
                        "BARRIER_STAGE_MISMATCH",
                    )
                    marker.pop("token")
                    write_json(output / "barrier.json", marker)
                    outside = await snapshot(core, tracking, event_id, inbox_id, shipment_id)
                    require(outside == initial, "PRECOMMIT_EFFECT_VISIBLE")
                    require(child.poll() is None, "CHILD_EXITED_BEFORE_INTERVENTION")
                    write_json(output / "outside-barrier.json", outside)
                if action == "kill":
                    result["stage"] = "intervention"
                    result["kill"] = await hard_kill(child, seconds=15)
                    require(result["kill"]["killed"] is True, "KILL_NOT_CONFIRMED")
                    require(result["kill"]["exit_code"] != 0, "UNEXPECTED_KILL_EXIT")
                    result["intervention_confirmed"] = True
                    result["stage"] = "rollback"
                    async with asyncio.timeout(30):
                        while True:
                            after = await snapshot(core, tracking, event_id, inbox_id, shipment_id)
                            if after == initial:
                                target = core if owner == "core" else tracking
                                async with target.session() as session:
                                    active = await session.scalar(
                                        text(
                                            "SELECT count(*) FROM pg_stat_activity WHERE pid=:pid"
                                        ),
                                        {"pid": marker["backend_pid"]},
                                    )
                                if not active:
                                    break
                            await asyncio.sleep(0.1)
                    write_json(output / "after-kill.json", after)
                    require(after == initial, "ROLLBACK_MISMATCH")
                    await spawn(["-m", f"fulfillflow.{owner}.worker"], configs[owner], "recovery")
                else:
                    writer.write(b"release\n")
                    await writer.drain()
                    result["intervention_confirmed"] = True
                if owner == "core":
                    await spawn(["-m", "fulfillflow.tracking.worker"], configs["tracking"], "peer")
                result["stage"] = "recovery"
                async with asyncio.timeout(60):
                    while True:
                        current = await snapshot(core, tracking, event_id, inbox_id, shipment_id)
                        live = [p for p, _ in children if p is not child or action != "kill"]
                        require(all(p.poll() is None for p in live), "RECOVERY_PROCESS_EXITED")
                        if complete(current):
                            break
                        await asyncio.sleep(0.1)
                check_complete(current, initial)
                write_json(output / "recovered.json", current)
                require(result["webhook_requests"] == 1, "REDELIVERY_DURING_RECOVERY")
                result["stage"] = "idempotency"
                duplicate = await post_event()
                require(
                    duplicate.status_code == 200 and duplicate.json()["result"] == "DUPLICATE",
                    "DUPLICATE_RESPONSE",
                )
                final = await snapshot(core, tracking, event_id, inbox_id, shipment_id)
                require(final == current, "DUPLICATE_CHANGED_CONFIRMED_RESULT")
                write_json(output / "duplicate.json", final)
                result.update(status="PASS", stage="complete")
    except BaseException as exc:
        result["status"] = (
            "FAIL"
            if result["intervention_confirmed"] and isinstance(exc, (AssertionError, TimeoutError))
            else "INCONCLUSIVE"
            if result["intervention_confirmed"]
            else "INVALID"
        )
        result["errors"].append(controlled_error(exc, result["stage"]))
        if isinstance(exc, AssertionError) and exc.args and isinstance(exc.args[0], str):
            code = exc.args[0]
            if code.replace("_", "").isalnum() and code.isupper():
                result["errors"][-1]["assertion"] = code
    finally:
        if writer:
            writer.close()
        if server:
            server.close()
            await server.wait_closed()
        cleanup_deadline = min(begun + 300, time.monotonic() + 30)
        for process, drain_task in reversed(children):
            try:
                remaining = max(0.1, cleanup_deadline - time.monotonic())
                cleanup_kill = None
                if process.poll() is None:
                    cleanup_kill = await hard_kill(process, seconds=min(5, remaining))
                remaining = max(0.1, cleanup_deadline - time.monotonic())
                result["processes"][
                    next(
                        i
                        for i, entry in enumerate(result["processes"])
                        if entry["pid"] == process.pid
                    )
                ].update(
                    exit_code=process.poll(),
                    cleanup_kill=cleanup_kill,
                    output=await asyncio.wait_for(drain_task, min(5, remaining)),
                )
            except BaseException as exc:
                result["cleanup_errors"].append(controlled_error(exc, "shutdown"))
        for db in (core, tracking):
            if db:
                try:
                    await asyncio.wait_for(
                        db.dispose(), max(0.1, min(5, cleanup_deadline - time.monotonic()))
                    )
                except BaseException as exc:
                    result["cleanup_errors"].append(controlled_error(exc, "shutdown"))
        if result["cleanup_errors"] and result["status"] == "PASS":
            result["status"] = "INCONCLUSIVE"
        result["elapsed_seconds"] = round(time.monotonic() - begun, 3)
        result["ended_at"] = datetime.now(UTC).isoformat()
        try:
            write_json(output / "result.json", result)
        except BaseException as exc:
            result["export_errors"].append(controlled_error(exc, "export"))
            if result["status"] == "PASS":
                result["status"] = "INCONCLUSIVE"
            print(
                json.dumps(
                    {
                        "scenario_primary": result["errors"],
                        "scenario_cleanup": result["cleanup_errors"],
                        "export": result["export_errors"],
                    }
                ),
                file=sys.stderr,
            )
            raise ScenarioExportError(result) from None
    return result

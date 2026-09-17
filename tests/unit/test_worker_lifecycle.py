"""Loop failure and bounded shutdown must never leave a healthy partial worker."""

import asyncio
import json
import time

import pytest

from fulfillflow.messaging.health import Heartbeat, healthy
from fulfillflow.messaging.telemetry import LOGGER, emit
from fulfillflow.messaging.worker import pause, supervise


@pytest.mark.parametrize(
    "service,stages",
    [
        ("core", ("publish", "publish_notifications", "receive", "process")),
        ("tracking", ("publish", "receive", "process")),
        ("notifications", ("receive", "process")),
    ],
)
def test_health_requires_fresh_ready_required_loops(tmp_path, service, stages):
    heartbeat = Heartbeat(tmp_path / "health.json", service)
    assert set(heartbeat.stages) == set(stages)
    heartbeat.write()
    assert not healthy(heartbeat.path, service)
    for stage in stages:
        heartbeat.record(stage, "ready")
    heartbeat.write()
    assert healthy(heartbeat.path, service)
    assert not healthy(heartbeat.path, "unknown-service")
    assert not healthy(heartbeat.path, service, now=time.monotonic() + 46)
    heartbeat.record("receive", "dependency_unavailable")
    heartbeat.write()
    assert not healthy(heartbeat.path, service)
    heartbeat.path.write_text("invalid")
    assert not healthy(heartbeat.path, service)


@pytest.mark.parametrize("state", ["starting", "dependency_unavailable"])
def test_core_health_requires_both_publishers(tmp_path, state):
    heartbeat = Heartbeat(tmp_path / "health.json", "core")
    for stage in heartbeat.stages:
        heartbeat.record(stage, "ready")
    heartbeat.record("publish_notifications", state)
    heartbeat.write()
    assert not healthy(heartbeat.path, "core")
    heartbeat.record("publish_notifications", "ready")
    heartbeat.write()
    assert healthy(heartbeat.path, "core")


@pytest.mark.parametrize("failure", [True, False])
async def test_required_loop_failure_or_unexpected_return_stops_peers(tmp_path, failure):
    stopped = asyncio.Event()
    cleaned = asyncio.Event()
    heartbeat = Heartbeat(tmp_path / "health.json", "core")

    async def broken():
        await asyncio.sleep(0)
        if failure:
            raise RuntimeError("not externally logged")

    async def peer():
        try:
            await stopped.wait()
        finally:
            cleaned.set()

    with pytest.raises(RuntimeError, match="REQUIRED_LOOP_STOPPED"):
        await supervise({"broken": broken, "peer": peer}, stopped, heartbeat, grace=0.2)
    assert cleaned.is_set()
    assert not healthy(heartbeat.path, "core")


async def test_shutdown_finishes_current_work_or_cancels_at_deadline(tmp_path):
    stop = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    heartbeat = Heartbeat(tmp_path / "health.json", "core")
    task = asyncio.create_task(supervise({"process": blocked}, stop, heartbeat, grace=0.1))
    await started.wait()
    stop.set()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set()
    assert not healthy(heartbeat.path, "core")


async def test_dependency_wait_is_interruptible():
    stop = asyncio.Event()
    task = asyncio.create_task(pause(stop, 30))
    stop.set()
    await asyncio.wait_for(task, 0.1)


def test_logs_allowlist_never_emits_body_or_credentials(caplog, monkeypatch):
    monkeypatch.setattr(LOGGER, "propagate", True)
    with caplog.at_level("INFO", logger=LOGGER.name):
        emit(
            "core",
            "process",
            "BLOCKED",
            category="APPLICATION_CONFLICT",
            message={"event_id": "test-id", "body": "SECRET", "password": "SECRET"},
        )
    record = json.loads(caplog.records[-1].message)
    assert record["event_id"] == "test-id"
    assert "SECRET" not in caplog.text
    assert record["category"] == "APPLICATION_CONFLICT"


async def test_shutdown_allows_current_operation_to_commit(tmp_path):
    stop = asyncio.Event()
    started = asyncio.Event()
    committed = asyncio.Event()

    async def operation():
        started.set()
        await stop.wait()
        await asyncio.sleep(0.02)
        committed.set()

    heartbeat = Heartbeat(tmp_path / "health.json", "tracking")
    task = asyncio.create_task(supervise({"process": operation}, stop, heartbeat, grace=2))
    await started.wait()
    stop.set()
    await asyncio.wait_for(task, 1)
    assert committed.is_set()


def test_active_heartbeat_cannot_hide_stale_loop(tmp_path):
    heartbeat = Heartbeat(tmp_path / "health.json", "core")
    for stage in heartbeat.stages:
        heartbeat.record(stage, "ready")
    heartbeat.stages["process"]["at"] -= 46
    heartbeat.write()
    assert not healthy(heartbeat.path, "core")

"""Notifications process/database outages do not undo or stall Core/Tracking completion."""

import asyncio
import json
import os
import subprocess

import httpx
import pytest

from fulfillflow.messaging.health import healthy
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from tests.e2e.test_external_simulator_journey import (
    _available_port,
    _start_application,
    _stop_application,
    _wait_until_ready,
)
from tests.e2e.test_notification_recovery import eventually, scalar, start
from tests.operations_support import owner_environment
from tests.service_pair import create_app

pytestmark = pytest.mark.integration


async def notifications_login(enabled):
    container = os.environ["TEST_V11_POSTGRES_CONTAINER"]
    assert "test" in container
    sql = "ALTER ROLE fulfillflow_notifications " + ("LOGIN" if enabled else "NOLOGIN")
    if not enabled:
        sql += (
            "; SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename='fulfillflow_notifications'"
        )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("outage", ["api", "worker", "database"])
async def test_real_notifications_outage_and_return_preserve_tracking_and_order(
    postgres_settings,
    postgres_database,
    postgres_tracking_settings,
    postgres_notifications_database,
    postgres_notifications_settings,
    fixed_clock,
    tmp_path,
    outage,
):
    port = _available_port()
    url = f"http://127.0.0.1:{port}"
    settings = postgres_settings.model_copy(update={"notifications_base_url": url})
    environment = owner_environment(postgres_notifications_settings)
    environment.update(APP_HOST="127.0.0.1", APP_PORT=str(port))
    api = _start_application(environment)
    children = [api]
    heartbeat = tmp_path / "notifications.json"
    try:
        await _wait_until_ready(api, url)
        app = create_app(settings, postgres_database, clock=fixed_clock)
        app.state.notifications_transport = None
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
        ):
            core = start(settings, tmp_path / "core.json")
            tracking = start(postgres_tracking_settings, tmp_path / "tracking.json")
            notification_worker = start(postgres_notifications_settings, heartbeat)
            children += [core, tracking, notification_worker]

            async def ready():
                return healthy(heartbeat, "notifications")

            await eventually(ready)
            if outage == "api":
                await asyncio.to_thread(_stop_application, api)
            elif outage == "worker":
                await asyncio.to_thread(_stop_application, notification_worker)
            else:
                await notifications_login(False)

                async def paused():
                    data = json.loads(heartbeat.read_text())
                    return data["stages"]["process"]["state"] == "dependency_unavailable"

                await eventually(paused)
                assert notification_worker.poll() is None
            shipment = await _create_shipment(
                client, reference="OUTAGE", carrier_code="carrier-alpha", tracking_code="OUTAGE"
            )
            accepted = await _post_event(
                client,
                settings,
                fixed_clock,
                carrier_code="carrier-alpha",
                event_id="outage-event",
                raw_body=_alpha_body("outage-event", "OUTAGE", status="DELIVERED"),
            )
            assert accepted.status_code == 202

            async def completed():
                assert core.poll() is None and tracking.poll() is None
                return (await client.get(accepted.headers["location"])).json()[
                    "status"
                ] == "PROCESSED"

            await eventually(completed)
            assert (await client.get(f"/api/v1/shipments/{shipment}")).json()[
                "status"
            ] == "DELIVERED"
            assert await scalar(postgres_database, "SELECT status FROM orders") == "FULFILLED"
            observed = await client.get("/api/v1/notifications")
            if outage in ("api", "database"):
                assert observed.status_code == 503
            else:
                assert observed.status_code == 200 and observed.json()["total"] == 0
            if outage == "database":
                await notifications_login(True)
            elif outage == "api":
                api = _start_application(environment)
                children.append(api)
                await _wait_until_ready(api, url)
            else:
                notification_worker = start(postgres_notifications_settings, heartbeat)
                children.append(notification_worker)

            async def simulated():
                result = await client.get("/api/v1/notifications")
                return result.status_code == 200 and result.json()["total"] == 1

            await eventually(simulated, seconds=45)
            assert (
                await scalar(postgres_notifications_database, "SELECT attempts FROM message_inbox")
                == 1
            )
            assert (
                await scalar(postgres_notifications_database, "SELECT status FROM notifications")
                == "SIMULATED"
            )
    finally:
        if outage == "database":
            await notifications_login(True)
        for child in reversed(children):
            await asyncio.to_thread(_stop_application, child)

"""Complete public-HTTP journey driven by the independent Carrier simulator."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import AsyncClient, HTTPError
from scripts.prepare_demo_v13 import prepare

from fulfillflow.config import Settings
from fulfillflow.db import Database


@pytest.mark.integration
@pytest.mark.parametrize(
    "carrier_code,tracking_code,prefix,secret_setting,secret_variable",
    [
        (
            "carrier-alpha",
            "E2EALPHA0001",
            "e2e-alpha",
            "carrier_alpha_webhook_secret",
            "CARRIER_ALPHA_WEBHOOK_SECRET",
        ),
        (
            "carrier-beta",
            "E2EBETA0001",
            "e2e-beta",
            "carrier_beta_webhook_secret",
            "CARRIER_BETA_WEBHOOK_SECRET",
        ),
    ],
)
async def test_external_simulator_updates_the_complete_operational_ui(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_settings: Settings,
    postgres_notifications_settings: Settings,
    postgres_notifications_database: Database,
    carrier_code: str,
    tracking_code: str,
    prefix: str,
    secret_setting: str,
    secret_variable: str,
) -> None:
    del postgres_database  # Owns schema cleanup; the live app opens an independent engine.
    del postgres_notifications_database
    async with _live_application(
        postgres_settings, postgres_tracking_settings, postgres_notifications_settings
    ) as base_url:
        async with AsyncClient(base_url=base_url, timeout=10) as client:
            first_demo = await prepare(client)
            assert await prepare(client) == first_demo
            assert len(first_demo["shipments"]) == 2
            order_response = await client.post(
                "/api/v1/orders",
                json={
                    "external_reference": "E2E-SIMULATOR-0001",
                    "recipient": {
                        "name": "External Simulator Recipient",
                        "email": "e2e-simulator@example.test",
                        "postal_code": "09700-000",
                        "city": "São Bernardo do Campo",
                        "state": "SP",
                    },
                },
            )
            assert order_response.status_code == 201, order_response.text
            order = order_response.json()
            confirm = await client.post(f"/api/v1/orders/{order['id']}/confirm")
            assert confirm.status_code == 200, confirm.text
            shipment_response = await client.post(
                "/api/v1/shipments",
                json={
                    "order_id": order["id"],
                    "carrier_code": carrier_code,
                    "tracking_code": tracking_code,
                    "estimated_delivery_date": "2026-09-05",
                },
            )
            assert shipment_response.status_code == 201, shipment_response.text
            shipment = shipment_response.json()

        simulator_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.endswith("DATABASE_URL")
            and key not in ("CARRIER_ALPHA_WEBHOOK_SECRET", "CARRIER_BETA_WEBHOOK_SECRET")
        }
        carrier_secret = getattr(postgres_tracking_settings, secret_setting).get_secret_value()
        simulator_environment[secret_variable] = carrier_secret
        completed = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "scripts/simulate_carrier_events.py",
                "--base-url",
                base_url,
                "--carrier",
                carrier_code,
                "--tracking-code",
                shipment["tracking_code"],
                "--scenario",
                "valid",
                "--seed",
                "20260831",
                "--event-id-prefix",
                prefix,
                "--start-at",
                "2026-08-31T12:00:00Z",
                "--timeout-seconds",
                "5",
                "--completion-timeout-seconds",
                "10",
            ],
            cwd=Path.cwd(),
            env=simulator_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        records = [json.loads(line) for line in completed.stdout.splitlines()]
        accepted = [item for item in records if item.get("phase") == "accepted"]
        assert len(accepted) == 4
        observations = [item for item in records if item.get("phase") != "accepted"]
        assert len(observations) == 4
        assert [item["result"] for item in observations] == ["APPLIED"] * 4
        assert all(item["success"] is True for item in observations)
        assert observations[-1]["current_status"] == "DELIVERED"
        assert "sha256=" not in completed.stdout
        assert carrier_secret not in completed.stdout

        async with AsyncClient(base_url=base_url, timeout=10) as client:
            # Tracking completion does not imply that Notifications has consumed its fact yet.
            async with asyncio.timeout(15):
                while True:
                    notification_response = await client.get(
                        "/api/v1/notifications", params={"shipment_id": shipment["id"]}
                    )
                    assert notification_response.status_code == 200, notification_response.text
                    notification_records = notification_response.json()["items"]
                    if len(notification_records) == 4:
                        break
                    await asyncio.sleep(0.1)
            assert all(item["status"] == "SIMULATED" for item in notification_records)
            assert len({item["tracking_event_id"] for item in notification_records}) == 4
            assert all(
                item["recipient"] == "e2e-simulator@example.test" for item in notification_records
            )
            (
                order_page,
                shipment_page,
                timeline,
                inbox,
                notifications,
                dashboard,
            ) = await asyncio.gather(
                client.get(f"/orders/{order['id']}"),
                client.get(f"/shipments/{shipment['id']}"),
                client.get(f"/shipments/{shipment['id']}/tracking"),
                client.get("/carrier-events"),
                client.get("/notifications"),
                client.get("/"),
            )

        assert order_page.status_code == 200
        assert shipment_page.status_code == 200
        assert timeline.status_code == 200
        assert inbox.status_code == 200
        assert notifications.status_code == 200
        assert dashboard.status_code == 200
        assert "FULFILLED" in order_page.text
        assert "DELIVERED" in shipment_page.text
        assert timeline.text.count("APPLIED") >= 4
        for suffix in ("valid-01", "valid-02", "valid-03", "valid-04"):
            assert f"{prefix}-{suffix}" in inbox.text
        assert notifications.text.count("SIMULATED") >= 4
        assert "Simulation recorded" in notifications.text
        assert "Recent carrier inbox events" in dashboard.text
        assert f"{prefix}-valid-04" in dashboard.text


@asynccontextmanager
async def _live_application(
    settings: Settings, tracking: Settings, notifications: Settings
) -> AsyncIterator[str]:
    port = _available_port()
    tracking_port = _available_port()
    while tracking_port == port:
        tracking_port = _available_port()
    notifications_port = _available_port()
    while notifications_port in (port, tracking_port):
        notifications_port = _available_port()
    environment = {
        key: value for key, value in os.environ.items() if not key.endswith("DATABASE_URL")
    }
    environment.update(
        {
            "APP_ENV": "test",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "DATABASE_URL": settings.database_dsn,
            "SERVICE_ROLE": "core",
            "INTERNAL_API_SECRET": settings.internal_api_secret.get_secret_value(),
            "CORE_BASE_URL": f"http://127.0.0.1:{port}",
            "TRACKING_BASE_URL": f"http://127.0.0.1:{tracking_port}",
            "NOTIFICATIONS_BASE_URL": f"http://127.0.0.1:{notifications_port}",
            "NOTIFICATIONS_API_SECRET": settings.notifications_api_secret.get_secret_value(),
            "SESSION_SECRET": settings.session_secret.get_secret_value(),
            "CARRIER_ALPHA_WEBHOOK_SECRET": (
                settings.carrier_alpha_webhook_secret.get_secret_value()
            ),
            "CARRIER_BETA_WEBHOOK_SECRET": (
                settings.carrier_beta_webhook_secret.get_secret_value()
            ),
            "PYTHONUNBUFFERED": "1",
            "AMQP_URL": os.environ["TEST_AMQP_URL"],
        }
    )
    tracking_environment = dict(environment)
    tracking_environment.update(
        SERVICE_ROLE="tracking",
        APP_PORT=str(tracking_port),
        DATABASE_URL=tracking.database_dsn,
        CARRIER_ALPHA_WEBHOOK_SECRET=tracking.carrier_alpha_webhook_secret.get_secret_value(),
        CARRIER_BETA_WEBHOOK_SECRET=tracking.carrier_beta_webhook_secret.get_secret_value(),
    )
    tracking_environment.pop("SESSION_SECRET", None)
    environment.pop("CARRIER_ALPHA_WEBHOOK_SECRET", None)
    environment.pop("CARRIER_BETA_WEBHOOK_SECRET", None)
    notifications_environment = dict(environment)
    notifications_environment.update(
        SERVICE_ROLE="notifications",
        APP_PORT=str(notifications_port),
        DATABASE_URL=notifications.database_dsn,
        INTERNAL_API_SECRET=settings.notifications_api_secret.get_secret_value(),
    )
    notifications_environment.pop("SESSION_SECRET", None)
    processes: list[subprocess.Popen[str]] = []
    base_url = f"http://127.0.0.1:{port}"
    try:
        for env, url in (
            (tracking_environment, f"http://127.0.0.1:{tracking_port}"),
            (notifications_environment, f"http://127.0.0.1:{notifications_port}"),
            (environment, base_url),
        ):
            process = await asyncio.to_thread(_start_application, env)
            processes.append(process)
            await _wait_until_ready(process, url)
        for env in (environment, tracking_environment, notifications_environment):
            processes.append(
                await asyncio.to_thread(
                    _start_application, env, f"fulfillflow.{env['SERVICE_ROLE']}.worker"
                )
            )
        try:
            yield base_url
        except BaseException:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                _stdout, stderr = await asyncio.to_thread(process.communicate, timeout=16)
                print("Process diagnostic:", stderr[-3000:])
            raise
    finally:
        for process in reversed(processes):
            await asyncio.to_thread(_stop_application, process)


def _start_application(
    environment: Mapping[str, str], module: str | None = None
) -> subprocess.Popen[str]:
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            module
            or {
                "core": "fulfillflow",
                "tracking": "fulfillflow.tracking",
                "notifications": "fulfillflow.notifications",
            }[environment["SERVICE_ROLE"]],
        ],
        cwd=Path.cwd(),
        env=dict(environment),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creation_flags,
    )


async def _wait_until_ready(
    process: subprocess.Popen[str],
    base_url: str,
) -> None:
    async with AsyncClient(timeout=1) as client:
        for _ in range(100):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"Live application exited early: {stderr or stdout}")
            try:
                response = await client.get(f"{base_url}/health/ready")
                if response.status_code == 200:
                    return
            except (HTTPError, OSError):
                pass
            await asyncio.sleep(0.1)
    raise AssertionError("Live application did not become ready within 10 seconds")


def _stop_application(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=16)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])

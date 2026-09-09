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
from scripts.prepare_demo_v11 import prepare

from fulfillflow.config import Settings
from fulfillflow.db import Database


@pytest.mark.integration
async def test_external_simulator_updates_the_complete_operational_ui(
    postgres_settings: Settings,
    postgres_database: Database,
    postgres_tracking_settings: Settings,
) -> None:
    del postgres_database  # Owns schema cleanup; the live app opens an independent engine.
    async with _live_application(postgres_settings, postgres_tracking_settings) as base_url:
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
                    "carrier_code": "carrier-alpha",
                    "tracking_code": "E2EALPHA0001",
                    "estimated_delivery_date": "2026-09-05",
                },
            )
            assert shipment_response.status_code == 201, shipment_response.text
            shipment = shipment_response.json()

        simulator_environment = dict(os.environ)
        simulator_environment.pop("DATABASE_URL", None)
        simulator_environment.pop("TEST_DATABASE_URL", None)
        simulator_environment.pop("CARRIER_BETA_WEBHOOK_SECRET", None)
        simulator_environment["CARRIER_ALPHA_WEBHOOK_SECRET"] = (
            postgres_settings.carrier_alpha_webhook_secret.get_secret_value()
        )
        completed = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "scripts/simulate_carrier_events.py",
                "--base-url",
                base_url,
                "--carrier",
                "carrier-alpha",
                "--tracking-code",
                shipment["tracking_code"],
                "--scenario",
                "valid",
                "--seed",
                "20260831",
                "--event-id-prefix",
                "e2e-alpha",
                "--start-at",
                "2026-08-31T12:00:00Z",
                "--timeout-seconds",
                "5",
            ],
            cwd=Path.cwd(),
            env=simulator_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        observations = [json.loads(line) for line in completed.stdout.splitlines()]
        assert len(observations) == 4
        assert [item["result"] for item in observations] == ["APPLIED"] * 4
        assert all(item["success"] is True for item in observations)
        assert observations[-1]["current_status"] == "DELIVERED"
        assert "sha256=" not in completed.stdout
        assert (
            postgres_settings.carrier_alpha_webhook_secret.get_secret_value()
            not in completed.stdout
        )

        async with AsyncClient(base_url=base_url, timeout=10) as client:
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
            assert f"e2e-alpha-{suffix}" in inbox.text
        assert notifications.text.count("SIMULATED") >= 4
        assert "Simulation recorded" in notifications.text
        assert "Recent carrier inbox events" in dashboard.text
        assert "e2e-alpha-valid-04" in dashboard.text


@asynccontextmanager
async def _live_application(settings: Settings, tracking: Settings) -> AsyncIterator[str]:
    port = _available_port()
    tracking_port = _available_port()
    while tracking_port == port:
        tracking_port = _available_port()
    environment = dict(os.environ)
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
            "SESSION_SECRET": settings.session_secret.get_secret_value(),
            "CARRIER_ALPHA_WEBHOOK_SECRET": (
                settings.carrier_alpha_webhook_secret.get_secret_value()
            ),
            "CARRIER_BETA_WEBHOOK_SECRET": (
                settings.carrier_beta_webhook_secret.get_secret_value()
            ),
            "PYTHONUNBUFFERED": "1",
        }
    )
    tracking_environment = dict(environment)
    tracking_environment.update(
        SERVICE_ROLE="tracking", APP_PORT=str(tracking_port), DATABASE_URL=tracking.database_dsn
    )
    tracking_environment.pop("SESSION_SECRET", None)
    environment.pop("CARRIER_ALPHA_WEBHOOK_SECRET", None)
    environment.pop("CARRIER_BETA_WEBHOOK_SECRET", None)
    processes: list[subprocess.Popen[str]] = []
    base_url = f"http://127.0.0.1:{port}"
    try:
        for env, url in (
            (tracking_environment, f"http://127.0.0.1:{tracking_port}"),
            (environment, base_url),
        ):
            process = await asyncio.to_thread(_start_application, env)
            processes.append(process)
            await _wait_until_ready(process, url)
        yield base_url
    finally:
        for process in reversed(processes):
            await asyncio.to_thread(_stop_application, process)


def _start_application(environment: Mapping[str, str]) -> subprocess.Popen[str]:
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "fulfillflow.tracking" if environment["SERVICE_ROLE"] == "tracking" else "fulfillflow",
        ],
        cwd=Path.cwd(),
        env=dict(environment),
        stdout=subprocess.PIPE,
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
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])

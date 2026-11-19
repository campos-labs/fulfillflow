"""Test support: real Keycloak browser login against the application under test."""

import asyncio
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import uvicorn
from pydantic import AnyHttpUrl

from fulfillflow.access.browser import browser_flow
from fulfillflow.access.service import AccessService


async def authenticated_client(
    client, app, *, username="operator", enable=True, journey=browser_flow
):
    credentials_path = os.environ.get("TEST_IDENTITY_CREDENTIALS")
    if not credentials_path:
        raise RuntimeError("TEST_IDENTITY_CREDENTIALS must name prepared real Keycloak accounts")
    credentials = json.loads(
        await asyncio.to_thread(Path(credentials_path).read_text, encoding="utf-8")
    )
    settings = app.state.settings
    settings.oidc_callback_url = AnyHttpUrl("http://localhost:19800/auth/callback")
    app.state.oidc.callback_url = str(settings.oidc_callback_url)
    if enable:
        async with app.state.database.session() as session, session.begin():
            await AccessService(session, app.state.clock).administer(
                issuer=str(settings.oidc_issuer).rstrip("/"),
                subject=str(uuid5(NAMESPACE_URL, "fulfillflow:v2:" + username)),
                display_name="Synthetic " + username,
                role=username,
                enabled=True,
                executor="integration-tests",
                reason="Explicit synthetic test enablement",
            )
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=19800, lifespan="off", access_log=False, log_level="warning"
        )
    )
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(300):
            if server.started:
                break
            if server_task.done():
                await server_task
                raise RuntimeError("Test HTTP server did not start")
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("Test HTTP server startup")
        result = await asyncio.to_thread(
            journey, "http://localhost:19800", username, credentials["users"][username]
        )
        if result is None or not enable:
            return result
        assert result["session_status"] == 200
        for cookie in result["cookies"]:
            if cookie["name"] == "fulfillflow_session":
                client.cookies.set(cookie["name"], cookie["value"])
        client.headers["X-CSRF-Token"] = result["session"]["csrf_token"]
        return result
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 15)

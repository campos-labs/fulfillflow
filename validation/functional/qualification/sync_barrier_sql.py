"""Development-only SQL visibility qualification using each frozen version's ASGI fixtures.

No hard-kill, TCP transport or evaluated C outcome is claimed by these tests.
"""

import asyncio
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from tests.api.test_tracking import _alpha_body, _create_shipment, _post_event
from validation.functional.c_runtime import save_record
from validation.functional.c_sync_barrier import installed

pytestmark = pytest.mark.integration


async def test_real_sql_visibility_at_sync_hook(postgres_settings, postgres_database, fixed_clock):
    tag = os.environ["C_REFERENCE"]
    if tag == "v1.0.0":
        from fulfillflow.main import create_app
    else:
        from tests.service_pair import create_app
    app = create_app(postgres_settings, postgres_database, clock=fixed_clock)
    observed = []

    async def state(session):
        return {
            "notifications": await session.scalar(text("SELECT count(*) FROM notifications")),
            "shipment": await session.scalar(text("SELECT status FROM shipments")),
            "order": await session.scalar(text("SELECT status FROM orders")),
        }

    async def barrier(session):
        local = await state(session)
        async with postgres_database.session() as independent:
            outside = await state(independent)
        assert local == {"notifications": 1, "shipment": "DELIVERED", "order": "FULFILLED"}
        assert outside == {"notifications": 0, "shipment": "PENDING", "order": "CONFIRMED"}
        observed.append({"local": local, "independent": outside})

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _create_shipment(
                client, reference="c-hook", carrier_code="carrier-alpha", tracking_code="C-HOOK"
            )
            with installed(tag, "c-hook-event", barrier):
                response = await _post_event(
                    client,
                    postgres_settings,
                    fixed_clock,
                    carrier_code="carrier-alpha",
                    event_id="c-hook-event",
                    raw_body=_alpha_body("c-hook-event", "C-HOOK", status="DELIVERED"),
                )
            assert response.status_code == 200
            assert len(observed) == 1
            async with postgres_database.session() as session:
                final = await state(session)
            assert final == observed[0]["local"]
    path = Path(os.environ["C_QUALIFICATION_OUTPUT"])
    await asyncio.to_thread(
        save_record,
        path,
        {
            "tag": tag,
            "status": "SQL_HOOK_QUALIFIED_ONLY",
            "observed": observed,
            "final": final,
            "transport": "ASGI",
            "hard_kill": False,
            "fixture_cleanup": "frozen fixtures clear only newly allocated databases",
        },
    )

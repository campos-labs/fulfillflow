"""One native API process, optionally gated at the qualified synchronous SQL boundary."""

from __future__ import annotations

import asyncio
import json
import os
import selectors
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy import text
from validation.functional.c_observer import CompletionObserver
from validation.functional.c_sync_barrier import installed
from validation.functional.support import InstrumentationFailure, verify_application_source


async def main() -> None:
    from fulfillflow.config import Settings

    if os.environ["SERVICE_ROLE"] == "tracking":
        from fulfillflow.tracking.app import create_app
    else:
        from fulfillflow.main import create_app
    verify_application_source(Path(os.environ["FUNCTIONAL_APPLICATION_SOURCE"]))
    settings = Settings(_env_file=None)
    app = create_app(settings)

    async def barrier(session: Any) -> None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT pg_backend_pid() AS backend,pg_current_xact_id()::text AS xid,"
                        "(SELECT count(*) FROM notifications) AS notifications,"
                        "(SELECT status FROM shipments) AS shipment,"
                        "(SELECT status FROM orders) AS order_state"
                    )
                )
            )
            .mappings()
            .one()
        )
        if (row["notifications"], row["shipment"], row["order_state"]) != (
            1,
            "DELIVERED",
            "FULFILLED",
        ):
            raise InstrumentationFailure("probe_instrumentation_failed")
        reader, writer = await asyncio.open_connection("127.0.0.1", int(os.environ["BARRIER_PORT"]))
        try:
            marker = {
                "marker": "sql_before_commit",
                "pid": os.getpid(),
                "token": os.environ["BARRIER_TOKEN"],
                "event_id": os.environ["TARGET_EXTERNAL_ID"],
                "local": dict(row),
            }
            writer.write(json.dumps(marker).encode() + b"\n")
            await writer.drain()
            if await asyncio.wait_for(reader.readline(), 30) != b"release\n":
                raise InstrumentationFailure("probe_barrier_not_released")
        finally:
            writer.close()
            await writer.wait_closed()

    gate = (
        installed(os.environ["C_REFERENCE"], os.environ["TARGET_EXTERNAL_ID"], barrier)
        if (os.environ.get("C_GATE") == "1")
        else nullcontext()
    )
    with gate:
        server = uvicorn.Server(
            uvicorn.Config(
                CompletionObserver(app, os.environ["C_OBSERVER_TOKEN"])
                if os.environ.get("C_OBSERVER_TOKEN")
                else app,
                host="127.0.0.1",
                port=int(os.environ["C_PORT"]),
                log_config=None,
            )
        )
        await server.serve()


if __name__ == "__main__":
    with asyncio.Runner(
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    ) as runner:
        runner.run(main())

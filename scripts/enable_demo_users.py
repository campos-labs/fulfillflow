"""Explicitly enable the two synthetic local accounts in the configured Core database."""

import argparse
from uuid import NAMESPACE_URL, uuid5

from fulfillflow.access.service import AccessService
from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.db import Database
from fulfillflow.shared import SystemClock


async def enable(executor: str) -> None:
    settings = Settings()
    database = Database.from_settings(settings)
    try:
        async with database.session() as session, session.begin():
            for role in ("operator", "viewer"):
                await AccessService(session, SystemClock()).administer(
                    issuer=str(settings.oidc_issuer).rstrip("/"),
                    subject=str(uuid5(NAMESPACE_URL, "fulfillflow:v2:" + role)),
                    display_name="Synthetic " + role,
                    role=role,
                    enabled=True,
                    executor=executor,
                    reason="Explicit local demonstration enablement",
                )
    finally:
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", required=True)
    arguments = parser.parse_args()
    run_async(enable(arguments.executor))
    print("Synthetic viewer and operator enabled with audit")

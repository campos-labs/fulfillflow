"""Real process helpers with only the owning database in the child environment."""

import asyncio
import os
import subprocess
import sys


def owner_environment(settings):
    environment = {
        key: value for key, value in os.environ.items() if not key.endswith("DATABASE_URL")
    }
    environment.update(
        APP_ENV="test",
        SERVICE_ROLE=settings.service_role,
        DATABASE_URL=settings.database_dsn,
        INTERNAL_API_SECRET=settings.internal_api_secret.get_secret_value(),
    )
    if settings.service_role == "tracking":
        environment.update(
            CARRIER_ALPHA_WEBHOOK_SECRET=settings.carrier_alpha_webhook_secret.get_secret_value(),
            CARRIER_BETA_WEBHOOK_SECRET=settings.carrier_beta_webhook_secret.get_secret_value(),
        )
    return environment


async def command(settings, module, *arguments, deadline_seconds=25):
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", module, *map(str, arguments)],
        env=owner_environment(settings),
        capture_output=True,
        text=True,
        timeout=deadline_seconds,
        check=False,
    )

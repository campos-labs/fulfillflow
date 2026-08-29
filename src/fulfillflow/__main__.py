"""Container and module entry point."""

import uvicorn

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.main import create_app


def run() -> None:
    """Start one Uvicorn process with validated bind settings."""
    settings = Settings()
    config = uvicorn.Config(
        create_app(settings),
        host=settings.app_host,
        port=settings.app_port,
        workers=1,
    )
    run_async(uvicorn.Server(config).serve())


if __name__ == "__main__":
    run()

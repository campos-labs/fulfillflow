"""Core command receiver, local processor and result publisher entrypoint."""

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.core.message_handler import apply_command
from fulfillflow.core.message_tables import tables
from fulfillflow.messaging.worker import serve


def main() -> None:
    settings = Settings()
    if settings.service_role != "core":
        raise ValueError("Core worker requires SERVICE_ROLE=core")
    try:
        run_async(serve(settings, tables, apply_command))
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

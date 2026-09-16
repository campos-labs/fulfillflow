"""Tracking command publisher, result receiver and local finalizer entrypoint."""

import logging

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.messaging.worker import serve
from fulfillflow.tracking.message_handler import apply_result
from fulfillflow.tracking.message_tables import tables


def main() -> None:
    settings = Settings()
    if settings.service_role != "tracking":
        raise ValueError("Tracking worker requires SERVICE_ROLE=tracking")
    try:
        run_async(serve(settings, tables, apply_result))
    except KeyboardInterrupt:
        return
    except Exception:
        logging.getLogger(__name__).error("tracking worker stopped: required loop unavailable")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

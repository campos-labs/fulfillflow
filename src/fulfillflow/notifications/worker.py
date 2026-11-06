"""Notifications fact receiver and SQL-only simulation processor entrypoint."""

from fulfillflow.asyncio_support import run_async
from fulfillflow.config import Settings
from fulfillflow.messaging.worker import serve
from fulfillflow.notifications.message_handler import apply_notification
from fulfillflow.notifications.message_tables import tables


def main() -> None:
    settings = Settings()
    if settings.service_role != "notifications":
        raise ValueError("Notifications worker requires SERVICE_ROLE=notifications")
    try:
        run_async(serve(settings, tables, apply_notification))
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

"""Prevent one-use OIDC credentials from entering Uvicorn access logs."""

import logging


class CallbackQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            target = args[2]
            if isinstance(target, str) and target.split("?", 1)[0] == "/auth/callback":
                record.args = (*args[:2], "/auth/callback", *args[3:])
        return True


def protect_callback_logs() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, CallbackQueryFilter) for item in logger.filters):
        logger.addFilter(CallbackQueryFilter())

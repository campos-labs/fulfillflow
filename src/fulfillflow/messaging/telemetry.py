"""Allowlisted structured worker records; never format exception or payload text."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger("fulfillflow.messages")


def configure() -> None:
    if not LOGGER.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)
    LOGGER.propagate = False
    LOGGER.setLevel(logging.INFO)
    # AMQP library errors may interpolate credential-bearing connection URLs.
    for name in ("aio_pika", "aiormq"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False


def emit(
    service: str,
    stage: str,
    outcome: str,
    *,
    duration: float = 0,
    category: str | None = None,
    message: dict[str, Any] | None = None,
) -> None:
    fields = {
        key: value
        for key, value in (message or {}).items()
        if key
        in (
            "message_id",
            "event_id",
            "correlation_id",
            "request_id",
            "attempts",
            "generation",
        )
    }
    LOGGER.info(
        json.dumps(
            dict(
                timestamp=datetime.now(UTC).isoformat(),
                service=service,
                stage=stage,
                outcome=outcome,
                category=category,
                duration_ms=round(duration * 1000, 3),
                **fields,
            ),
            default=str,
        )
    )

"""Strict, autoescaped Jinja environment and HTML response helpers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    pass_context,
    select_autoescape,
)
from jinja2.runtime import Context
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from fulfillflow.config import Settings

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"

environment = Environment(
    loader=FileSystemLoader(TEMPLATE_ROOT),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
    undefined=StrictUndefined,
)


def _path_for(request: Request, name: str, **path_params: Any) -> str:
    """Build a relative application path without reflecting the Host header."""
    return str(request.app.url_path_for(name, **path_params))


@pass_context
def _display_value(context: Context, value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        request = context.get("request")
        if (
            isinstance(request, Request)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        ):
            settings = cast(Settings, request.app.state.settings)
            value = value.astimezone(ZoneInfo(settings.app_timezone))
        return value.isoformat(timespec="seconds")
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _status_class(value: object) -> str:
    normalized = str(value.value) if isinstance(value, Enum) else str(value)
    return {
        "APPLIED": "text-bg-success",
        "NO_STATE_CHANGE": "text-bg-info",
        "IGNORED_STALE": "text-bg-warning",
        "IGNORED_INVALID_TRANSITION": "text-bg-danger",
        "SIMULATED": "text-bg-success",
        "FAILED": "text-bg-danger",
        "PROCESSED": "text-bg-success",
        "RECEIVED": "text-bg-info",
        "REJECTED": "text-bg-danger",
        "DELIVERED": "text-bg-success",
        "FULFILLED": "text-bg-success",
        "CANCELLED": "text-bg-secondary",
        "EXCEPTION": "text-bg-danger",
        "RETURNED": "text-bg-dark",
        "OUT_FOR_DELIVERY": "text-bg-primary",
        "IN_TRANSIT": "text-bg-info",
        "POSTED": "text-bg-info",
        "CONFIRMED": "text-bg-primary",
        "CREATED": "text-bg-secondary",
        "PENDING": "text-bg-secondary",
    }.get(normalized, "text-bg-secondary")


environment.globals["path_for"] = _path_for
environment.filters["display"] = _display_value
environment.filters["status_class"] = _status_class
templates = Jinja2Templates(env=environment)

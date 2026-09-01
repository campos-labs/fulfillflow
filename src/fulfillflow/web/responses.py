"""HTML rendering, redirects and security response policy for the web UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from starlette.responses import RedirectResponse, Response

from fulfillflow.web.security import load_web_session, persist_web_session
from fulfillflow.web.templating import templates

_FLASH_MESSAGES = {
    "order-created": "Order created.",
    "order-confirmed": "Order confirmed.",
    "order-cancelled": "Order cancelled.",
    "shipment-created": "Shipment created.",
    "shipment-cancelled": "Shipment cancelled.",
}

_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    )
)


def is_htmx(request: Request) -> bool:
    """Recognize only the documented true-valued HTMX request header."""
    return request.headers.get("HX-Request", "").casefold() == "true"


def render_page(
    request: Request,
    content_template: str,
    *,
    title: str,
    section: str,
    context: Mapping[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    """Render a complete page or the matching main fragment from one route."""
    session = load_web_session(request)
    values: dict[str, Any] = {
        "title": title,
        "section": section,
        "content_template": content_template,
        "csrf_token": session.csrf_token,
        "flash_message": _FLASH_MESSAGES.get(session.flash or ""),
    }
    if context is not None:
        values.update(context)
    name = "fragment.html" if is_htmx(request) else "page.html"
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context=values,
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "private, no-store"
    persist_web_session(request, response, flash=None)
    _append_vary(response, "HX-Request")
    return response


def render_problem(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
) -> Response:
    """Render a sanitized operational error with the correlated request ID."""
    return render_page(
        request,
        "content/error.html",
        title=title,
        section="",
        status_code=status_code,
        context={
            "problem": {
                "status": status_code,
                "code": code,
                "title": title,
                "detail": detail,
                "request_id": request.state.request_id,
                "errors": errors or [],
            }
        },
    )


def redirect_with_flash(
    request: Request,
    route_name: str,
    *,
    flash: str,
    path_params: Mapping[str, Any] | None = None,
) -> Response:
    """Complete a mutation with PRG and signed deterministic flash state."""
    url = request.app.url_path_for(route_name, **dict(path_params or {}))
    response = RedirectResponse(str(url), status_code=303)
    persist_web_session(request, response, flash=flash)
    return response


def apply_security_headers(response: Response) -> None:
    """Apply the v1 browser hardening headers without changing API state."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)


def _append_vary(response: Response, value: str) -> None:
    existing = response.headers.get("Vary")
    values = {item.strip() for item in existing.split(",")} if existing else set()
    values.add(value)
    response.headers["Vary"] = ", ".join(sorted(values))

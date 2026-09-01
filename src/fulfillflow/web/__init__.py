"""Server-rendered operational interface composition helpers."""

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import Response

from fulfillflow.web.responses import apply_security_headers, render_problem
from fulfillflow.web.router import router
from fulfillflow.web.static import FulfillFlowStaticFiles

WEB_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = WEB_ROOT / "static"


def install_web(application: FastAPI) -> None:
    """Mount package-owned assets and register the HTML routes once."""
    application.state.web_problem_renderer = render_problem

    @application.middleware("http")
    async def browser_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        apply_security_headers(response)
        return response

    if not any(getattr(route, "name", None) == "web-static" for route in application.routes):
        application.mount(
            "/static",
            FulfillFlowStaticFiles(directory=STATIC_ROOT),
            name="web-static",
        )
    application.include_router(router)


__all__ = ["install_web", "router"]

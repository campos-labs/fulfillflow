"""Core browser authentication boundary and explicit public-route exceptions."""

import hmac
import re
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import FastAPI, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from fulfillflow.access.service import (
    AccessDeniedError,
    AccessService,
    AuthenticationRequiredError,
    OperationForbiddenError,
    csrf_token,
)
from fulfillflow.db import Database
from fulfillflow.http.problems import _problem

SESSION_COOKIE = "fulfillflow_session"
_WEBHOOK = re.compile(r"/api/v1/carriers/[^/]+/events\Z")


async def access_problem(request: Request, exception: Exception) -> Response:
    unauthenticated = isinstance(exception, AuthenticationRequiredError)
    response = _problem(
        request,
        status_code=401 if unauthenticated else 403,
        code="AUTHENTICATION_REQUIRED" if unauthenticated else "ACCESS_FORBIDDEN",
        title="Sessão necessária" if unauthenticated else "Acesso negado",
        detail=(
            "Entre novamente para continuar. Nenhuma operação será repetida automaticamente."
            if unauthenticated
            else "Seu acesso não permite esta operação ou o token CSRF é inválido."
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def install_access(application: FastAPI) -> None:
    application.add_exception_handler(AccessDeniedError, access_problem)

    @application.middleware("http")
    async def authenticate(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if (
            path in {"/auth/login", "/auth/callback", "/health/live", "/health/ready", "/metrics"}
            or path.startswith(("/static/", "/internal/"))
            or (request.method == "POST" and _WEBHOOK.fullmatch(path))
        ):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            return await access_problem(request, AuthenticationRequiredError())
        database = cast(Database, request.app.state.database)
        try:
            async with database.session() as session, session.begin():
                identity = await AccessService(session, request.app.state.clock).authenticate(token)
            request.state.access_token = token
            request.state.access_identity = identity
            mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
            if path in {"/orders/new", "/shipments/new"} and identity.role != "operator":
                raise OperationForbiddenError()
            if mutation:
                if path != "/auth/logout" and identity.role != "operator":
                    raise OperationForbiddenError()
                # HTML business forms enforce CSRF in their bounded, strict form reader.
                # JSON APIs and HTMX require the header independently of form fields.
                if path.startswith("/api/") or request.headers.get("HX-Request"):
                    provided = request.headers.getlist("X-CSRF-Token")
                    if len(provided) != 1 or not hmac.compare_digest(
                        provided[0].encode(), csrf_token(token).encode()
                    ):
                        raise OperationForbiddenError()
        except AccessDeniedError as error:
            return await access_problem(request, error)
        except SQLAlchemyError:
            return _problem(
                request,
                status_code=503,
                code="ACCESS_UNAVAILABLE",
                title="Acesso indisponível",
                detail="Não foi possível verificar sua sessão.",
            )
        return await call_next(request)

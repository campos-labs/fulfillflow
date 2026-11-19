"""OIDC login and local opaque-session lifecycle."""

from typing import Annotated, cast

import httpx
from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Request
from joserfc.errors import JoseError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from fulfillflow.access.flows import consume_flow, save_flow
from fulfillflow.access.http import SESSION_COOKIE
from fulfillflow.access.oidc import OIDCClient
from fulfillflow.access.service import (
    AccessService,
    AuthenticationRequiredError,
    OperationForbiddenError,
    csrf_token,
)
from fulfillflow.api.dependencies import get_session
from fulfillflow.config import Settings
from fulfillflow.contracts.problems import ServiceProblemError
from fulfillflow.web.forms import FormBoundaryError, read_strict_form
from fulfillflow.web.security import verify_csrf

router = APIRouter(prefix="/auth", tags=["access"])
Session = Annotated[AsyncSession, Depends(get_session)]
FLOW_COOKIE = "fulfillflow_login"


@router.get("/login")
async def login(request: Request, session: Session) -> Response:
    request.scope["session"] = {}
    client = cast(OIDCClient, request.app.state.oidc)
    try:
        response = await client.login(request)
    except (httpx.HTTPError, OAuthError, ValueError):
        raise ServiceProblemError(
            status_code=503,
            code="IDENTITY_UNAVAILABLE",
            title="Login indisponível",
            detail="Tente entrar novamente.",
        ) from None
    handle = await save_flow(session, request.app.state.clock, request.session)
    response.set_cookie(
        FLOW_COOKIE,
        handle,
        max_age=600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback")
async def callback(request: Request, session: Session) -> Response:
    request.scope["session"] = await consume_flow(
        session,
        request.app.state.clock,
        request.cookies.get(FLOW_COOKIE, ""),
    )
    client = cast(OIDCClient, request.app.state.oidc)
    try:
        issuer, subject = await client.identity(request)
    except (httpx.HTTPError, OAuthError, JoseError, ValueError, KeyError):
        raise AuthenticationRequiredError("Invalid login callback") from None
    settings = cast(Settings, request.app.state.settings)
    async with session.begin():
        token = await AccessService(session, request.app.state.clock).login(
            issuer,
            subject,
            lifetime=settings.access_session_seconds,
        )
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/auth")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.access_session_seconds,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/session")
async def current_session(request: Request) -> Response:
    return JSONResponse(
        {
            "expires_at": request.state.access_identity.expires_at.isoformat(),
            "csrf_token": csrf_token(request.state.access_token),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/logout")
async def logout(request: Request, session: Session) -> Response:
    provided = request.headers.get("X-CSRF-Token")
    if provided is None:
        try:
            provided = (await read_strict_form(request, fields=())).csrf_token
        except FormBoundaryError:
            raise OperationForbiddenError() from None
    if not verify_csrf(request, provided):
        raise OperationForbiddenError()
    async with session.begin():
        await AccessService(session, request.app.state.clock).logout(request.state.access_token)
    response = HTMLResponse(
        '<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
        "<title>Sessão encerrada</title><main><h1>Sessão encerrada</h1>"
        '<p>Seu acesso local foi encerrado.</p><a href="/auth/login">Entrar novamente</a>'
        "</main></html>"
    )
    response.delete_cookie(SESSION_COOKIE)
    response.headers["Cache-Control"] = "no-store"
    return response

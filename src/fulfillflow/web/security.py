"""Session-bound CSRF and separate signed presentation-only flash cookies."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import cast

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.responses import Response

from fulfillflow.access.service import csrf_token
from fulfillflow.config import Settings

SESSION_COOKIE = "fulfillflow_session"
FLASH_COOKIE = "fulfillflow_flash"
SESSION_MAX_AGE_SECONDS = 28800


@dataclass(frozen=True)
class WebSession:
    csrf_token: str
    issued_at: int
    flash: str | None = None


def _serializer(request: Request) -> URLSafeTimedSerializer:
    settings = cast(Settings, request.app.state.settings)
    return URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(),
        salt="fulfillflow-flash-v2",
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def load_web_session(request: Request) -> WebSession:
    token = getattr(request.state, "access_token", "")
    flash = None
    encoded = request.cookies.get(FLASH_COOKIE)
    if encoded:
        try:
            value = _serializer(request).loads(encoded, max_age=60)
            if isinstance(value, str):
                flash = value
        except BadSignature:
            pass
    return WebSession(csrf_token(token) if token else "", 0, flash)


def verify_csrf(request: Request, provided_token: str | None) -> bool:
    token = getattr(request.state, "access_token", "")
    return bool(token) and hmac.compare_digest(
        (provided_token or "").encode(),
        csrf_token(token).encode(),
    )


def persist_web_session(
    request: Request,
    response: Response,
    *,
    flash: str | None = None,
) -> WebSession:
    # Rendering never creates or renews authentication state.
    if flash:
        response.set_cookie(
            FLASH_COOKIE,
            _serializer(request).dumps(flash),
            max_age=60,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
        )
    elif FLASH_COOKIE in request.cookies:
        response.delete_cookie(FLASH_COOKIE)
    return load_web_session(request)

"""Small signed-session CSRF mechanism for the unauthenticated local UI."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from time import time as _unix_time
from typing import Any, cast

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import Response

from fulfillflow.config import Settings

SESSION_COOKIE = "fulfillflow_session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
_SESSION_SALT = "fulfillflow-web-session-v1"


@dataclass(frozen=True, slots=True)
class WebSession:
    """Minimal signed client session; it carries no identity or secret."""

    csrf_token: str
    issued_at: int
    flash: str | None = None


def load_web_session(request: Request) -> WebSession:
    """Verify the signed cookie or issue fresh unpredictable session state."""
    cached = getattr(request.state, "web_session", None)
    if isinstance(cached, WebSession):
        return cached

    encoded = request.cookies.get(SESSION_COOKIE)
    if encoded is not None:
        try:
            payload = _serializer(request).loads(encoded, max_age=SESSION_MAX_AGE_SECONDS)
            session = _validated_payload(payload, now=_request_timestamp(request))
            request.state.web_session = session
            return session
        except (BadSignature, SignatureExpired, ValueError, TypeError):
            pass

    session = _new_session(_request_timestamp(request))
    request.state.web_session = session
    return session


def verify_csrf(request: Request, provided_token: str | None) -> bool:
    """Compare the form token with signed session state in constant time."""
    expected = load_web_session(request).csrf_token.encode("utf-8")
    provided = (provided_token or "").encode("utf-8")
    return hmac.compare_digest(provided, expected)


def persist_web_session(
    request: Request,
    response: Response,
    *,
    flash: str | None = None,
) -> WebSession:
    """Attach signed state using cookie controls required by the UI contract."""
    current = load_web_session(request)
    # Early revocation would require server-side state, outside the stateless v1.0 model.
    session = WebSession(
        csrf_token=current.csrf_token,
        issued_at=current.issued_at,
        flash=flash,
    )
    remaining = _remaining_lifetime(session, now=_request_timestamp(request))
    encoded = _serializer(request).dumps(
        {
            "csrf_token": session.csrf_token,
            "issued_at": session.issued_at,
            "flash": session.flash,
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        encoded,
        max_age=remaining,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    request.state.web_session = session
    return session


def _serializer(request: Request) -> URLSafeTimedSerializer:
    settings = cast(Settings, request.app.state.settings)
    return URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(),
        salt=_SESSION_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def _validated_payload(payload: Any, *, now: int) -> WebSession:
    if not isinstance(payload, dict) or set(payload) != {"csrf_token", "issued_at", "flash"}:
        raise ValueError("invalid session payload")
    csrf_token = payload["csrf_token"]
    issued_at = payload["issued_at"]
    flash = payload["flash"]
    if not isinstance(csrf_token, str) or len(csrf_token) < 32:
        raise ValueError("invalid CSRF state")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise ValueError("invalid session issue time")
    if issued_at < 0 or issued_at > now:
        raise ValueError("invalid session issue time")
    if now - issued_at >= SESSION_MAX_AGE_SECONDS:
        raise ValueError("expired session")
    if flash is not None and not isinstance(flash, str):
        raise ValueError("invalid flash state")
    return WebSession(csrf_token=csrf_token, issued_at=issued_at, flash=flash)


def _new_session(now: int) -> WebSession:
    return WebSession(csrf_token=secrets.token_urlsafe(32), issued_at=now)


def _request_timestamp(request: Request) -> int:
    cached = getattr(request.state, "web_session_timestamp", None)
    if isinstance(cached, int) and not isinstance(cached, bool):
        return cached
    timestamp = int(_unix_time())
    request.state.web_session_timestamp = timestamp
    return timestamp


def _remaining_lifetime(session: WebSession, *, now: int) -> int:
    remaining = SESSION_MAX_AGE_SECONDS - (now - session.issued_at)
    if not 1 <= remaining <= SESSION_MAX_AGE_SECONDS:
        raise ValueError("invalid session lifetime")
    return remaining

"""OIDC adapter rejects callbacks before any local session can be issued."""

import pytest
from authlib.integrations.base_client.errors import OAuthError
from starlette.requests import Request

from fulfillflow.access.oidc import OIDCClient


@pytest.mark.parametrize("query", [b"", b"code=untrusted", b"code=untrusted&state=unknown"])
async def test_callback_without_matching_login_state_is_rejected(query):
    client = OIDCClient("http://127.0.0.1:19999/realms/test", "test", "http://localhost/callback")
    request = Request({"type": "http", "method": "GET", "query_string": query, "session": {}})
    with pytest.raises(OAuthError):
        await client.identity(request)


async def test_pkce_is_enabled_and_password_grant_is_not_used():
    client = OIDCClient("http://127.0.0.1:19999/realms/test", "test", "http://localhost/callback")
    assert client.client.client_kwargs["code_challenge_method"] == "S256"
    assert client.client.client_kwargs["scope"] == "openid profile"


def test_callback_query_is_removed_from_access_log():
    import logging

    from fulfillflow.access.logging import CallbackQueryFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s %s %s %s %s",
        ("client", "GET", "/auth/callback?code=SECRET&state=PRIVATE", "1.1", 303),
        None,
    )
    assert CallbackQueryFilter().filter(record)
    assert record.getMessage() == "client GET /auth/callback 1.1 303"

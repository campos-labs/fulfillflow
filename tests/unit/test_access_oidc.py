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


@pytest.mark.parametrize("variation", ["valid", "issuer", "audience", "nonce", "signature"])
async def test_signed_provider_response_is_bound_to_client_and_login(monkeypatch, variation):
    import time

    from joserfc import jwt
    from joserfc.errors import JoseError
    from joserfc.jwk import RSAKey

    issuer = "https://identity.example.test/realms/example"
    client = OIDCClient(issuer, "fulfillflow", "https://app.example.test/auth/callback")
    trusted = RSAKey.generate_key(2048)
    signing_key = RSAKey.generate_key(2048) if variation == "signature" else trusted
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": "synthetic",
        "aud": ["fulfillflow", "account"],
        "azp": "fulfillflow",
        "iat": now,
        "exp": now + 300,
        "nonce": "login-nonce",
    }
    if variation == "issuer":
        claims["iss"] = "https://other.example.test"
    if variation == "audience":
        claims["aud"] = ["other-client"]
    if variation == "nonce":
        claims["nonce"] = "unrelated-login"
    signed = jwt.encode({"alg": "RS256"}, claims, signing_key)
    client.client.server_metadata.update(
        {
            "_loaded_at": now,
            "issuer": issuer,
            "jwks": {"keys": [trusted.as_dict(private=False)]},
        }
    )

    async def token_endpoint(**kwargs):
        assert kwargs["code_verifier"] == "temporary-verifier"
        return {"id_token": signed, "access_token": "synthetic"}

    monkeypatch.setattr(client.client, "fetch_access_token", token_endpoint)
    state = {}
    await client.client.framework.set_state_data(
        state,
        "bound",
        {
            "nonce": "login-nonce",
            "redirect_uri": client.callback_url,
            "code_verifier": "temporary-verifier",
        },
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "query_string": b"code=unit&state=bound",
            "session": state,
        }
    )
    if variation == "valid":
        assert await client.identity(request) == (issuer, "synthetic")
    else:
        with pytest.raises((ValueError, JoseError)):
            await client.identity(request)

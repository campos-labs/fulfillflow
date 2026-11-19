"""Authlib OIDC adapter. Temporary state is supplied by the HTTP boundary."""

from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from starlette.responses import Response


class OIDCClient:
    """Delegate PKCE, state, nonce and signed ID-token validation to Authlib."""

    def __init__(self, issuer: str, client_id: str, callback_url: str) -> None:
        self.issuer = issuer.rstrip("/")
        self.callback_url = callback_url
        registry = OAuth()
        self.client = registry.register(
            "identity",
            client_id=client_id,
            server_metadata_url=f"{self.issuer}/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid profile",
                "code_challenge_method": "S256",
                "token_endpoint_auth_method": "none",
                "trust_env": False,
                "timeout": 10,
            },
        )

    async def login(self, request: Request) -> Response:
        response: Response = await self.client.authorize_redirect(request, self.callback_url)
        return response

    async def identity(self, request: Request) -> tuple[str, str]:
        token: dict[str, Any] = await self.client.authorize_access_token(
            request,
            claims_options={"iss": {"essential": True, "value": self.issuer}},
        )
        claims = token.get("userinfo")
        if not claims or claims.get("iss") != self.issuer or not claims.get("sub"):
            raise ValueError("Invalid identity response")
        return self.issuer, str(claims["sub"])

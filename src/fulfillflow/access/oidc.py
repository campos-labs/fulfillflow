"""Authlib OIDC adapter. Temporary state is supplied by the HTTP boundary."""

from typing import Any

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from starlette.responses import Response


class OIDCClient:
    """Delegate PKCE, state, nonce and signed ID-token validation to Authlib."""

    def __init__(
        self, issuer: str, client_id: str, callback_url: str, *, backchannel_url: str | None = None
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.callback_url = callback_url
        self.backchannel_url = backchannel_url.rstrip("/") if backchannel_url else None
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
        await self.prepare()
        response: Response = await self.client.authorize_redirect(request, self.callback_url)
        return response

    async def identity(self, request: Request) -> tuple[str, str]:
        await self.prepare()
        token: dict[str, Any] = await self.client.authorize_access_token(
            request,
            claims_options={
                "iss": {"essential": True, "value": self.issuer},
                "aud": {"essential": True, "value": self.client_id},
            },
        )
        claims = token.get("userinfo")
        if (
            not token.get("id_token")
            or not claims
            or claims.get("iss") != self.issuer
            or not claims.get("sub")
        ):
            raise ValueError("Invalid identity response")
        return self.issuer, str(claims["sub"])

    async def prepare(self) -> None:
        if not self.backchannel_url or self.client.server_metadata.get("_loaded_at"):
            return
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(f"{self.backchannel_url}/.well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()
        if metadata.get("issuer") != self.issuer:
            raise ValueError("Unexpected discovery issuer")
        for key in ("token_endpoint", "jwks_uri", "userinfo_endpoint"):
            url = metadata.get(key, "")
            if not url.startswith(self.issuer + "/"):
                raise ValueError("Unexpected discovery endpoint")
            metadata[key] = self.backchannel_url + url[len(self.issuer) :]
        metadata["_loaded_at"] = True
        self.client.server_metadata.update(metadata)

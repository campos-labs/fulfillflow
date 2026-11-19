"""End-to-end HTTP access through real Keycloak and PostgreSQL."""

import httpx

from tests.access_support import authenticated_client


async def test_real_provider_identity_does_not_enable_itself(ui_application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui_application), base_url="http://testserver"
    ) as client:
        result = await authenticated_client(
            client, ui_application, username="disabled", enable=False
        )
        assert result["landing_status"] == 403
        assert result["session_status"] == 401
        assert not any(c["name"] == "fulfillflow_session" for c in result["cookies"])


async def test_callback_requires_browser_bound_one_use_flow(ui_application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui_application), base_url="http://testserver"
    ) as client:
        assert (await client.get("/auth/callback?code=invalid&state=unbound")).status_code == 401
        login = await client.get("/auth/login")
        assert login.status_code == 302
        assert "code_challenge_method=S256" in login.headers["location"]
        assert (await client.get("/auth/callback?code=invalid&state=unbound")).status_code == 401
        assert (await client.get("/auth/callback?code=invalid&state=unbound")).status_code == 401
        assert (await client.get("/auth/session")).status_code == 401


async def test_real_oidc_viewer_and_operator_logout(ui_application):
    for role in ("viewer", "operator"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui_application), base_url="http://testserver"
        ) as client:
            assert (await client.get("/api/v1/orders")).status_code == 401
            await authenticated_client(client, ui_application, username=role)
            assert (await client.get("/api/v1/orders")).status_code == 200
            state = await client.get("/auth/session")
            assert state.headers["cache-control"] == "no-store"
            assert set(state.json()) == {"expires_at", "csrf_token"}
            bad = await client.post("/api/v1/orders", json={}, headers={"X-CSRF-Token": "bad"})
            assert bad.status_code == 403
            response = await client.post("/api/v1/orders", json={})
            assert response.status_code == (403 if role == "viewer" else 422)
            logout = await client.post("/auth/logout")
            assert logout.status_code == 200
            assert "location" not in logout.headers
            assert "Sessão encerrada" in logout.text
            assert (await client.get("/auth/session")).status_code == 401

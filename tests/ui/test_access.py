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
            assert "SessÃ£o encerrada" in logout.text
            assert (await client.get("/auth/session")).status_code == 401


def _operate_in_browser(origin, username, password):
    from fulfillflow.access.browser import logged_in_page

    with logged_in_page(origin, username, password) as (page, status):
        assert status == 200
        assert page.goto(origin + "/orders").status == 200
        if username == "operator":
            page.get_by_role("link", name="Create Order").click()
            fields = {
                "external_reference": "BROWSER-ISSUE4",
                "recipient_name": "Synthetic",
                "recipient_email": "operator@example.test",
                "recipient_postal_code": "09700-000",
                "recipient_city": "Santos",
                "recipient_state": "SP",
            }
            for name, value in fields.items():
                page.locator("#" + name).fill(value)
            page.get_by_role("button", name="Create Order").click()
            page.get_by_role("heading", name="Order BROWSER-ISSUE4").wait_for()
            page.get_by_role("button", name="Confirm", exact=True).click()
            assert "CONFIRMED" in page.locator("main").inner_text()
        else:
            assert page.get_by_role("link", name="Create Order").count() == 0
            assert "BROWSER-ISSUE4" in page.locator("main").inner_text()
            assert page.goto(origin + "/orders/new").status == 403
        page.get_by_role("button", name="Sair").click()
        assert "Sessão encerrada" in page.title()
        assert page.context.request.get(origin + "/auth/session").status == 401


async def test_browser_operator_creates_and_viewer_consults_then_logs_out(ui_application):
    async with httpx.AsyncClient() as client:
        for role in ("operator", "viewer"):
            await authenticated_client(
                client, ui_application, username=role, journey=_operate_in_browser
            )

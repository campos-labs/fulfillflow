"""Real browser OIDC login for synthetic users; never accepts service tokens."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page


def browser_login(base_url: str, username: str, password: str) -> dict[str, Any]:
    """Use the same Keycloak form and callback as a person, in a fresh browser."""
    result = browser_flow(base_url, username, password)
    if result["session_status"] != 200:
        raise RuntimeError("Browser login did not produce an operational session")
    return result


def browser_flow(base_url: str, username: str, password: str) -> dict[str, Any]:
    """Follow the real login, preserving rejection status for integration checks."""
    with logged_in_page(base_url, username, password) as (page, status):
        response = page.context.request.get(base_url + "/auth/session")
        return {
            "cookies": [
                c for c in page.context.cookies(base_url) if c["name"] == "fulfillflow_session"
            ],
            "session": response.json(),
            "session_status": response.status,
            "landing_status": status,
        }


@contextmanager
def logged_in_page(
    base_url: str, username: str, password: str
) -> Iterator[tuple["Page", int | None]]:
    """Yield the landing page inside the actual signed-in browser context."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(base_url + "/auth/login")
            page.locator('input[name="username"]').fill(username)
            page.locator('input[name="password"]').fill(password)
            with page.expect_navigation(wait_until="domcontentloaded") as navigation:
                page.locator('[type="submit"]').click()
            callback_response = navigation.value
            yield page, callback_response.status if callback_response else None
        finally:
            browser.close()

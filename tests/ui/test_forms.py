"""HTML form validation, CSRF, PRG, idempotency and conflict coverage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.types import Message, Scope

from fulfillflow.web import security as web_security
from fulfillflow.web.forms import MAX_FORM_BODY_BYTES, FormBoundaryError, read_strict_form
from tests.ui.support import csrf_token

SESSION_TEST_T0 = 1_800_000_000


def _order_form(token: str, reference: str = "UI-FORM-0001") -> dict[str, str]:
    return {
        "csrf_token": token,
        "external_reference": reference,
        "recipient_name": "Form Recipient",
        "recipient_email": "form-recipient@example.test",
        "recipient_postal_code": "09700-000",
        "recipient_city": "São Bernardo do Campo",
        "recipient_state": "sp",
    }


@dataclass
class _ReceiveProbe:
    chunks: list[bytes]
    calls: int = 0

    async def __call__(self) -> Message:
        if self.calls >= len(self.chunks):
            raise AssertionError("the ASGI body stream was consumed past its final chunk")
        chunk = self.chunks[self.calls]
        self.calls += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": self.calls < len(self.chunks),
        }


def _streaming_form_request(
    probe: _ReceiveProbe,
    *,
    content_length: int | None = None,
) -> Request:
    return Request(_form_scope(content_length), receive=probe)


def _form_scope(content_length: int | None = None) -> Scope:
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/orders",
        "raw_path": b"/orders",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _client(application: FastAPI, *, https: bool = False) -> AsyncClient:
    scheme = "https" if https else "http"
    return AsyncClient(
        transport=ASGITransport(app=application),
        base_url=f"{scheme}://testserver",
        follow_redirects=False,
    )


def _session_serializer(application: FastAPI) -> URLSafeTimedSerializer:
    settings = application.state.settings
    return URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(),
        salt=web_security._SESSION_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def _session_payload(application: FastAPI, cookie: str) -> dict[str, Any]:
    payload = _session_serializer(application).loads(cookie)
    assert isinstance(payload, dict)
    return payload


def _cookie_max_age(response: Any) -> int:
    attributes = (item.strip() for item in response.headers["set-cookie"].split(";"))
    value = next(item for item in attributes if item.casefold().startswith("max-age="))
    return int(value.split("=", maxsplit=1)[1])


async def test_order_form_csrf_validation_prg_and_refresh_safety(
    ui_client: AsyncClient,
) -> None:
    form_page = await ui_client.get("/orders/new")
    token = csrf_token(form_page)
    set_cookie = form_page.headers["set-cookie"]
    assert "fulfillflow_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=28800" in set_cookie

    missing_csrf = await ui_client.post("/orders", data=_order_form(""))
    assert missing_csrf.status_code == 403
    assert missing_csrf.headers["content-type"].startswith("text/html")
    assert "CSRF_VALIDATION_FAILED" in missing_csrf.text
    assert "Request ID:" in missing_csrf.text

    invalid_csrf = await ui_client.post("/orders", data=_order_form("not-a-valid-token"))
    assert invalid_csrf.status_code == 403
    assert "CSRF_VALIDATION_FAILED" in invalid_csrf.text

    valid_token = csrf_token(await ui_client.get("/orders/new"))
    duplicate_csrf = urlencode(
        [
            ("csrf_token", valid_token),
            ("csrf_token", valid_token),
            *[
                (name, value)
                for name, value in _order_form(valid_token).items()
                if name != "csrf_token"
            ],
        ]
    )
    duplicate_response = await ui_client.post(
        "/orders",
        content=duplicate_csrf,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate_response.status_code == 403
    assert "CSRF_VALIDATION_FAILED" in duplicate_response.text

    token = csrf_token(await ui_client.get("/orders/new"))
    invalid = _order_form(token)
    invalid["recipient_email"] = "not-an-email"
    invalid_response = await ui_client.post("/orders", data=invalid)
    assert invalid_response.status_code == 422
    assert "email must contain a local part" in invalid_response.text
    assert "Request ID:" in invalid_response.text

    token = csrf_token(await ui_client.get("/orders/new"))
    extra = _order_form(token)
    extra["status"] = "FULFILLED"
    extra_response = await ui_client.post("/orders", data=extra)
    assert extra_response.status_code == 422
    assert "unexpected fields" in extra_response.text

    token = csrf_token(await ui_client.get("/orders/new"))
    created = await ui_client.post("/orders", data=_order_form(token))
    assert created.status_code == 303
    assert created.headers["location"].startswith("/orders/")

    detail = await ui_client.get(created.headers["location"])
    assert detail.status_code == 200
    assert "Order created." in detail.text
    assert "UI-FORM-0001" in detail.text
    assert "2026-08-29T09:00:00-03:00" in detail.text

    refreshed = await ui_client.get(created.headers["location"])
    assert "Order created." not in refreshed.text

    listed = await ui_client.get("/api/v1/orders")
    assert listed.json()["total"] == 1


async def test_csrf_token_remains_stable_for_legitimate_mutations_in_one_session(
    ui_client: AsyncClient,
) -> None:
    token = csrf_token(await ui_client.get("/orders/new"))

    first = await ui_client.post(
        "/orders",
        data=_order_form(token, reference="UI-STABLE-CSRF-0001"),
    )
    second = await ui_client.post(
        "/orders",
        data=_order_form(token, reference="UI-STABLE-CSRF-0002"),
    )
    conflict = await ui_client.post(
        "/orders",
        data=_order_form(token, reference="UI-STABLE-CSRF-0002"),
    )

    assert first.status_code == 303
    assert second.status_code == 303
    assert conflict.status_code == 409
    assert "RESOURCE_CONFLICT" in conflict.text
    assert csrf_token(await ui_client.get("/orders/new")) == token
    listed = await ui_client.get("/api/v1/orders")
    assert listed.json()["total"] == 2


async def test_session_absolute_age_survives_prg_and_repeated_cookie_reissues(
    ui_application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = SESSION_TEST_T0
    monkeypatch.setattr(web_security, "_unix_time", lambda: current_time)

    async with _client(ui_application) as browser:
        initial_page = await browser.get("/orders/new")
        original_token = csrf_token(initial_page)
        initial_cookie = browser.cookies[web_security.SESSION_COOKIE]
        initial_payload = _session_payload(ui_application, initial_cookie)
        assert initial_payload["issued_at"] == SESSION_TEST_T0
        assert initial_payload["csrf_token"] == original_token
        assert _cookie_max_age(initial_page) == web_security.SESSION_MAX_AGE_SECONDS

        current_time = SESSION_TEST_T0 + 7 * 60 * 60
        created = await browser.post(
            "/orders",
            data=_order_form(original_token, reference="UI-ABSOLUTE-SESSION-0001"),
        )
        assert created.status_code == 303
        assert _cookie_max_age(created) == 60 * 60
        prg_cookie = browser.cookies[web_security.SESSION_COOKIE]
        prg_payload = _session_payload(ui_application, prg_cookie)
        assert prg_payload == {
            "csrf_token": original_token,
            "issued_at": SESSION_TEST_T0,
            "flash": "order-created",
        }

        current_time = SESSION_TEST_T0 + 7 * 60 * 60 + 30 * 60
        detail = await browser.get(created.headers["location"])
        assert detail.status_code == 200
        assert "Order created." in detail.text
        assert csrf_token(detail) == original_token
        assert _cookie_max_age(detail) == 30 * 60
        consumed_flash_cookie = browser.cookies[web_security.SESSION_COOKIE]
        consumed_payload = _session_payload(ui_application, consumed_flash_cookie)
        assert consumed_payload == {
            "csrf_token": original_token,
            "issued_at": SESSION_TEST_T0,
            "flash": None,
        }

        current_time = SESSION_TEST_T0 + 7 * 60 * 60 + 45 * 60
        repeated_navigation = await browser.get("/orders/new")
        assert repeated_navigation.status_code == 200
        assert csrf_token(repeated_navigation) == original_token
        assert _cookie_max_age(repeated_navigation) == 15 * 60
        latest_cookie = browser.cookies[web_security.SESSION_COOKIE]
        latest_payload = _session_payload(ui_application, latest_cookie)
        assert latest_payload["issued_at"] == SESSION_TEST_T0
        assert latest_payload["csrf_token"] == original_token

    current_time = SESSION_TEST_T0 + web_security.SESSION_MAX_AGE_SECONDS + 1
    expired_cookie_header = {"Cookie": f"{web_security.SESSION_COOKIE}={latest_cookie}"}
    async with _client(ui_application) as mutation_client:
        rejected = await mutation_client.post(
            "/orders",
            data=_order_form(original_token, reference="UI-ABSOLUTE-SESSION-EXPIRED"),
            headers=expired_cookie_header,
        )
    assert rejected.status_code == 403
    assert "CSRF_VALIDATION_FAILED" in rejected.text

    async with _client(ui_application) as navigation_client:
        replacement = await navigation_client.get("/orders/new", headers=expired_cookie_header)
        replacement_cookie = navigation_client.cookies[web_security.SESSION_COOKIE]
    replacement_payload = _session_payload(ui_application, replacement_cookie)
    assert replacement.status_code == 200
    assert csrf_token(replacement) != original_token
    assert replacement_payload["issued_at"] == current_time
    assert replacement_payload["csrf_token"] == csrf_token(replacement)
    assert _cookie_max_age(replacement) == web_security.SESSION_MAX_AGE_SECONDS


@pytest.mark.parametrize(
    "payload",
    [
        {"csrf_token": "a" * 40, "flash": None},
        {"csrf_token": "a" * 40, "issued_at": True, "flash": None},
        {"csrf_token": "a" * 40, "issued_at": "1800000000", "flash": None},
        {"csrf_token": "a" * 40, "issued_at": 1_800_000_000.0, "flash": None},
        {"csrf_token": "a" * 40, "issued_at": -1, "flash": None},
        {"csrf_token": "a" * 40, "issued_at": SESSION_TEST_T0 + 1, "flash": None},
        {
            "csrf_token": "a" * 40,
            "issued_at": SESSION_TEST_T0 - web_security.SESSION_MAX_AGE_SECONDS,
            "flash": None,
        },
    ],
    ids=("missing", "boolean", "string", "float", "negative", "future", "expired"),
)
async def test_invalid_session_issue_time_is_replaced_on_navigation(
    ui_application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setattr(web_security, "_unix_time", lambda: SESSION_TEST_T0)
    old_token = str(payload["csrf_token"])
    invalid_cookie = _session_serializer(ui_application).dumps(payload)

    async with _client(ui_application) as browser:
        response = await browser.get(
            "/orders/new",
            headers={"Cookie": f"{web_security.SESSION_COOKIE}={invalid_cookie}"},
        )
        replacement_cookie = browser.cookies[web_security.SESSION_COOKIE]

    replacement_payload = _session_payload(ui_application, replacement_cookie)
    assert response.status_code == 200
    assert csrf_token(response) != old_token
    assert replacement_payload["issued_at"] == SESSION_TEST_T0
    assert replacement_payload["csrf_token"] == csrf_token(response)


async def test_csrf_token_is_bound_to_its_signed_browser_session(
    ui_application: FastAPI,
) -> None:
    async with _client(ui_application) as browser_a, _client(ui_application) as browser_b:
        token_a = csrf_token(await browser_a.get("/orders/new"))
        token_b = csrf_token(await browser_b.get("/orders/new"))
        assert token_a != token_b

        rejected_by_b = await browser_b.post(
            "/orders",
            data=_order_form(token_a, reference="UI-CROSS-SESSION-A"),
        )
        rejected_by_a = await browser_a.post(
            "/orders",
            data=_order_form(token_b, reference="UI-CROSS-SESSION-B"),
        )

    async with _client(ui_application) as browser_without_cookie:
        rejected_without_cookie = await browser_without_cookie.post(
            "/orders",
            data=_order_form(token_a, reference="UI-NO-SESSION-COOKIE"),
        )

    assert rejected_by_b.status_code == 403
    assert rejected_by_a.status_code == 403
    assert rejected_without_cookie.status_code == 403


async def test_tampered_session_cookie_rejects_the_old_token_and_get_starts_new_session(
    ui_client: AsyncClient,
    ui_application: FastAPI,
) -> None:
    token = csrf_token(await ui_client.get("/orders/new"))
    signed_cookie = ui_client.cookies[web_security.SESSION_COOKIE]
    tampered_cookie = f"{signed_cookie}tampered"

    async with _client(ui_application) as mutation_client:
        rejected = await mutation_client.post(
            "/orders",
            data=_order_form(token, reference="UI-TAMPERED-0001"),
            headers={"Cookie": f"{web_security.SESSION_COOKIE}={tampered_cookie}"},
        )
    async with _client(ui_application) as navigation_client:
        replacement = await navigation_client.get(
            "/orders/new",
            headers={"Cookie": f"{web_security.SESSION_COOKIE}={tampered_cookie}"},
        )

    assert rejected.status_code == 403
    assert "CSRF_VALIDATION_FAILED" in rejected.text
    assert replacement.status_code == 200
    assert csrf_token(replacement) != token


async def test_expired_session_cookie_rejects_the_old_token_and_get_starts_new_session(
    ui_application: FastAPI,
) -> None:
    class ExpiredTimestampSigner(TimestampSigner):
        def get_timestamp(self) -> int:
            return 1

    token = "expired-session-csrf-token-value-000000000000"
    settings = ui_application.state.settings
    serializer = URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(),
        salt=web_security._SESSION_SALT,
        signer=ExpiredTimestampSigner,
        signer_kwargs={"digest_method": hashlib.sha256},
    )
    expired_cookie = serializer.dumps({"csrf_token": token, "issued_at": 1, "flash": None})
    cookie_header = {"Cookie": f"{web_security.SESSION_COOKIE}={expired_cookie}"}

    async with _client(ui_application) as mutation_client:
        rejected = await mutation_client.post(
            "/orders",
            data=_order_form(token, reference="UI-EXPIRED-0001"),
            headers=cookie_header,
        )
    async with _client(ui_application) as navigation_client:
        replacement = await navigation_client.get("/orders/new", headers=cookie_header)

    assert rejected.status_code == 403
    assert "CSRF_VALIDATION_FAILED" in rejected.text
    assert replacement.status_code == 200
    assert csrf_token(replacement) != token


async def test_session_cookie_is_secure_only_over_https(ui_application: FastAPI) -> None:
    async with _client(ui_application, https=True) as browser:
        response = await browser.get("/orders/new")

    assert "Secure" in response.headers["set-cookie"]


async def test_created_order_can_be_cancelled_idempotently(
    ui_client: AsyncClient,
) -> None:
    token = csrf_token(await ui_client.get("/orders/new"))
    created = await ui_client.post(
        "/orders",
        data=_order_form(token, reference="UI-CANCEL-0001"),
    )
    order_path = created.headers["location"]

    page = await ui_client.get(order_path)
    cancelled = await ui_client.post(
        f"{order_path}/cancel",
        data={"csrf_token": csrf_token(page)},
    )
    assert cancelled.status_code == 303

    cancelled_page = await ui_client.get(order_path)
    assert "CANCELLED" in cancelled_page.text
    repeated = await ui_client.post(
        f"{order_path}/cancel",
        data={"csrf_token": token},
    )
    assert repeated.status_code == 303


async def test_form_reader_accepts_and_parses_exactly_32_kib() -> None:
    prefix = b"csrf_token=boundary-token&payload="
    body = prefix + b"x" * (MAX_FORM_BODY_BYTES - len(prefix))
    probe = _ReceiveProbe([body])

    submitted = await read_strict_form(
        _streaming_form_request(probe, content_length=len(body)),
        fields=("payload",),
    )

    assert len(body) == MAX_FORM_BODY_BYTES
    assert submitted.csrf_token == "boundary-token"
    assert submitted.fields["payload"] == "x" * (MAX_FORM_BODY_BYTES - len(prefix))
    assert probe.calls == 1


async def test_form_reader_rejects_one_byte_above_32_kib() -> None:
    probe = _ReceiveProbe([b"x" * (MAX_FORM_BODY_BYTES + 1)])

    with pytest.raises(FormBoundaryError, match="too large"):
        await read_strict_form(_streaming_form_request(probe), fields=())

    assert probe.calls == 1


async def test_form_reader_without_content_length_stops_before_the_last_chunk() -> None:
    probe = _ReceiveProbe(
        [
            b"a" * 16_000,
            b"b" * 16_000,
            b"c" * 1_000,
            b"must-not-be-requested",
        ]
    )

    with pytest.raises(FormBoundaryError, match="too large"):
        await read_strict_form(_streaming_form_request(probe), fields=())

    assert probe.calls == 3


async def test_oversized_stream_returns_sanitized_html_without_consuming_its_tail(
    ui_client: AsyncClient,
) -> None:
    requested_chunks: list[int] = []

    async def oversized_body():
        requested_chunks.append(1)
        yield b"a" * 16_000
        requested_chunks.append(2)
        yield b"b" * 17_000
        requested_chunks.append(3)
        yield b"must-not-be-requested"

    response = await ui_client.post(
        "/orders",
        content=oversized_body(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 422
    assert "The submitted form is too large." in response.text
    assert "Request ID:" in response.text
    assert "Traceback" not in response.text
    assert requested_chunks == [1, 2]


async def test_form_reader_ignores_false_small_content_length_and_stops_at_real_limit() -> None:
    probe = _ReceiveProbe(
        [
            b"a" * MAX_FORM_BODY_BYTES,
            b"b",
            b"must-not-be-requested",
        ]
    )

    with pytest.raises(FormBoundaryError, match="too large"):
        await read_strict_form(
            _streaming_form_request(probe, content_length=1),
            fields=(),
        )

    assert probe.calls == 2


async def test_form_reader_rejects_large_content_length_before_consuming_the_stream() -> None:
    probe = _ReceiveProbe([b"must-not-be-requested"])

    with pytest.raises(FormBoundaryError, match="too large"):
        await read_strict_form(
            _streaming_form_request(probe, content_length=MAX_FORM_BODY_BYTES + 1),
            fields=(),
        )

    assert probe.calls == 0


async def test_form_reader_does_not_consume_a_large_sentinel_tail() -> None:
    head = iter(
        (
            b"a" * (MAX_FORM_BODY_BYTES // 2),
            b"b" * (MAX_FORM_BODY_BYTES // 2),
            b"c",
        )
    )
    tail = (b"later-chunk" for _ in range(10_000))
    head_chunks_requested = 0
    tail_chunks_produced = 0

    async def lazy_receive() -> Message:
        nonlocal head_chunks_requested, tail_chunks_produced
        try:
            chunk = next(head)
        except StopIteration:
            tail_chunks_produced += 1
            chunk = next(tail)
        else:
            head_chunks_requested += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    with pytest.raises(FormBoundaryError, match="too large"):
        await read_strict_form(Request(_form_scope(), receive=lazy_receive), fields=())

    assert head_chunks_requested == 3
    assert tail_chunks_produced == 0


async def test_form_reader_translates_client_disconnect_to_sanitized_boundary_error() -> None:
    calls = 0

    async def disconnect() -> Message:
        nonlocal calls
        calls += 1
        return {"type": "http.disconnect"}

    request = Request(_form_scope(), receive=disconnect)
    with pytest.raises(FormBoundaryError, match="submission was interrupted"):
        await read_strict_form(request, fields=())

    assert calls == 1


async def test_order_actions_and_shipment_form_use_prg_and_surface_conflicts(
    ui_client: AsyncClient,
) -> None:
    token = csrf_token(await ui_client.get("/orders/new"))
    created = await ui_client.post(
        "/orders",
        data=_order_form(token, reference="UI-ACTIONS-0001"),
    )
    order_path = created.headers["location"]
    order_id = order_path.rsplit("/", maxsplit=1)[1]

    order_page = await ui_client.get(order_path)
    confirmed = await ui_client.post(
        f"{order_path}/confirm",
        data={"csrf_token": csrf_token(order_page)},
    )
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == order_path

    # Reuse the session token for a direct replay; the detail no longer offers Confirm.
    repeated_page = await ui_client.get(order_path)
    assert repeated_page.status_code == 200
    repeated = await ui_client.post(
        f"{order_path}/confirm",
        data={"csrf_token": token},
    )
    assert repeated.status_code == 303

    shipment_form = await ui_client.get(f"/shipments/new?order_id={order_id}")
    shipment_data = {
        "csrf_token": csrf_token(shipment_form),
        "order_id": order_id,
        "carrier_code": "carrier-alpha",
        "tracking_code": "ui-form-track-1",
        "estimated_delivery_date": "2026-09-05",
    }
    shipment = await ui_client.post("/shipments", data=shipment_data)
    assert shipment.status_code == 303
    assert shipment.headers["location"].startswith("/shipments/")
    shipment_path = shipment.headers["location"]

    empty_timeline = await ui_client.get(f"{shipment_path}/tracking")
    assert empty_timeline.status_code == 200
    assert "This Shipment has no Tracking events." in empty_timeline.text

    duplicate_form = await ui_client.get(f"/shipments/new?order_id={order_id}")
    shipment_data["csrf_token"] = csrf_token(duplicate_form)
    conflict = await ui_client.post("/shipments", data=shipment_data)
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("text/html")
    assert "RESOURCE_CONFLICT" in conflict.text
    assert "Request ID:" in conflict.text

    cancel_page = await ui_client.get(shipment_path)
    cancelled = await ui_client.post(
        f"{shipment_path}/cancel",
        data={"csrf_token": csrf_token(cancel_page)},
    )
    assert cancelled.status_code == 303
    assert cancelled.headers["location"] == shipment_path

    cancel_again_page = await ui_client.get(shipment_path)
    cancel_again = await ui_client.post(
        f"{shipment_path}/cancel",
        data={"csrf_token": csrf_token(cancel_again_page)},
    )
    assert cancel_again.status_code == 303

    conflict_page = await ui_client.get(order_path)
    assert conflict_page.status_code == 200
    invalid_cancel = await ui_client.post(
        f"{order_path}/cancel",
        data={"csrf_token": token},
    )
    assert invalid_cancel.status_code == 409
    assert "INVALID_ORDER_TRANSITION" in invalid_cancel.text

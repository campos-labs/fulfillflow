"""Independent contract tests for the external Carrier simulator."""

from __future__ import annotations

import ast
import hashlib
import hmac
import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from scripts import simulate_carrier_events as simulator

NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
SIGNED_TIMESTAMP = "1788188400"
ALPHA_SECRET = "alpha-simulator-test-secret"
BETA_SECRET = "beta-simulator-test-secret"


class FakeResponse:
    """Minimal bounded urllib response used without opening a socket."""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self._body[:amount] if amount >= 0 else self._body

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


@dataclass
class _RedirectRecorder:
    status: int
    location: str = ""
    request_count: int = 0
    redirected_request_count: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    raw_body: bytes = b""


@dataclass
class _TargetRecorder:
    request_count: int = 0


def _redirect_handler(recorder: _RedirectRecorder) -> type[BaseHTTPRequestHandler]:
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            recorder.request_count += 1
            recorder.headers = {name.casefold(): value for name, value in self.headers.items()}
            length = int(self.headers.get("Content-Length", "0"))
            recorder.raw_body = self.rfile.read(length)
            self.send_response(recorder.status)
            self.send_header("Location", recorder.location)
            self.end_headers()

        def do_GET(self) -> None:
            recorder.redirected_request_count += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"result":"APPLIED"}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return RedirectHandler


def _target_handler(recorder: _TargetRecorder) -> type[BaseHTTPRequestHandler]:
    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            recorder.request_count += 1
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:
            recorder.request_count += 1
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return TargetHandler


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _server_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def _redirect_cli_arguments(base_url: str) -> list[str]:
    return [
        "--base-url",
        base_url,
        "--carrier",
        simulator.CARRIER_ALPHA,
        "--tracking-code",
        "REDIRECT0001",
        "--scenario",
        simulator.SCENARIO_VALID,
        "--seed",
        "7",
        "--event-id-prefix",
        "redirect-test",
        "--start-at",
        "2026-08-31T15:00:00Z",
        "--timeout-seconds",
        "2",
    ]


def _config(
    carrier: str,
    scenario: str,
    *,
    secret: str | None = None,
) -> simulator.SimulatorConfig:
    return simulator.SimulatorConfig(
        base_url="http://127.0.0.1:8000",
        carrier=carrier,
        tracking_code="TRACK-0001",
        scenario=scenario,
        seed=20260831,
        prefix=f"test-{carrier}",
        start=NOW,
        timeout=2.5,
        secret=secret or (ALPHA_SECRET if carrier == simulator.CARRIER_ALPHA else BETA_SECRET),
    )


@pytest.mark.parametrize("carrier", simulator.CARRIERS)
def test_valid_scenario_builds_carrier_specific_full_flow(carrier: str) -> None:
    plan = simulator.build_scenario(
        _config(carrier, simulator.SCENARIO_VALID),
        signed_timestamp=SIGNED_TIMESTAMP,
    )

    assert [step.expected.result for step in plan.steps] == ["APPLIED"] * 4
    assert [step.expected.current_status for step in plan.steps] == [
        "POSTED",
        "IN_TRANSIT",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
    ]
    assert plan.steps[-1].expected.current_status == "DELIVERED"

    payloads = [json.loads(step.artifact.raw_body) for step in plan.steps]
    if carrier == simulator.CARRIER_ALPHA:
        assert [payload["status"] for payload in payloads] == [
            "CREATED",
            "MOVING",
            "OUT_FOR_DELIVERY",
            "DELIVERED",
        ]
        assert all(payload["trackingCode"] == "TRACK-0001" for payload in payloads)
        assert all(
            payload["eventId"] == step.artifact.event_id
            for payload, step in zip(payloads, plan.steps, strict=True)
        )
    else:
        assert [payload["event"]["type"] for payload in payloads] == [
            "label_created",
            "hub_scan",
            "courier_route",
            "completed",
        ]
        assert all(payload["tracking_number"] == "TRACK-0001" for payload in payloads)
        assert all(
            payload["id"] == step.artifact.event_id
            for payload, step in zip(payloads, plan.steps, strict=True)
        )


@pytest.mark.parametrize("carrier", simulator.CARRIERS)
def test_signed_bytes_headers_and_signature_are_exact(carrier: str) -> None:
    config = _config(carrier, simulator.SCENARIO_VALID)
    artifact = (
        simulator.build_scenario(
            config,
            signed_timestamp=SIGNED_TIMESTAMP,
        )
        .steps[0]
        .artifact
    )
    headers = dict(artifact.headers)

    signed_payload = (
        SIGNED_TIMESTAMP.encode("ascii")
        + b"."
        + artifact.event_id.encode("ascii")
        + b"."
        + artifact.raw_body
    )
    expected = (
        "sha256="
        + hmac.new(config.secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    )

    assert headers == {
        "Content-Type": "application/json",
        "X-FulfillFlow-Event-Id": artifact.event_id,
        "X-FulfillFlow-Timestamp": SIGNED_TIMESTAMP,
        "X-FulfillFlow-Signature": expected,
    }
    assert len({name.casefold() for name, _ in artifact.headers}) == 4
    assert artifact.timestamp == SIGNED_TIMESTAMP
    assert artifact.event_id.isascii()
    assert 1 <= len(artifact.event_id) <= 128
    assert all(0x21 <= ord(character) <= 0x7E for character in artifact.event_id)


@pytest.mark.parametrize("carrier", simulator.CARRIERS)
def test_duplicate_reuses_complete_request_artifact(carrier: str) -> None:
    plan = simulator.build_scenario(
        _config(carrier, simulator.SCENARIO_DUPLICATE),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    first, repeated = plan.steps

    assert repeated.artifact is first.artifact
    assert repeated.artifact.raw_body == first.artifact.raw_body
    assert repeated.artifact.event_id == first.artifact.event_id
    assert repeated.artifact.timestamp == first.artifact.timestamp
    assert repeated.artifact.headers == first.artifact.headers
    assert repeated.expected.result == "DUPLICATE"
    assert repeated.expected.original_result == "APPLIED"


@pytest.mark.parametrize("carrier", simulator.CARRIERS)
def test_out_of_order_and_negative_scenario_contracts(carrier: str) -> None:
    stale = simulator.build_scenario(
        _config(carrier, simulator.SCENARIO_OUT_OF_ORDER),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    newer_payload = json.loads(stale.steps[0].artifact.raw_body)
    older_payload = json.loads(stale.steps[1].artifact.raw_body)
    event_key = "eventDate" if carrier == simulator.CARRIER_ALPHA else "event"
    if event_key == "eventDate":
        newer_time = newer_payload[event_key]
        older_time = older_payload[event_key]
    else:
        newer_time = newer_payload[event_key]["occurred_at"]
        older_time = older_payload[event_key]["occurred_at"]
    assert older_time < newer_time
    assert stale.steps[1].expected.result == "IGNORED_STALE"

    unknown = simulator.build_scenario(
        _config(carrier, simulator.SCENARIO_UNKNOWN_STATUS),
        signed_timestamp=SIGNED_TIMESTAMP,
    ).steps[0]
    assert unknown.expected.status == 422
    assert unknown.expected.problem_code == "UNKNOWN_EXTERNAL_STATUS"

    invalid = simulator.build_scenario(
        _config(carrier, simulator.SCENARIO_INVALID_SIGNATURE),
        signed_timestamp=SIGNED_TIMESTAMP,
    ).steps[0]
    signature = dict(invalid.artifact.headers)["X-FulfillFlow-Signature"]
    correct = simulator.calculate_signature(
        _config(carrier, simulator.SCENARIO_INVALID_SIGNATURE).secret,
        timestamp=SIGNED_TIMESTAMP,
        event_id=invalid.artifact.event_id,
        raw_body=invalid.artifact.raw_body,
    )
    assert invalid.expected.status == 401
    assert invalid.expected.problem_code == "INVALID_WEBHOOK_SIGNATURE"
    assert signature != correct
    assert signature.startswith("sha256=")
    assert len(signature) == len("sha256=") + 64


def test_deterministic_inputs_produce_identical_request_plan() -> None:
    config = _config(simulator.CARRIER_BETA, simulator.SCENARIO_VALID)

    first = simulator.build_scenario(config, signed_timestamp=SIGNED_TIMESTAMP)
    second = simulator.build_scenario(config, signed_timestamp=SIGNED_TIMESTAMP)

    assert first == second


def test_send_request_transmits_the_precomputed_bytes_and_unique_headers() -> None:
    artifact = (
        simulator.build_scenario(
            _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
            signed_timestamp=SIGNED_TIMESTAMP,
        )
        .steps[0]
        .artifact
    )
    captured: dict[str, object] = {}
    response = FakeResponse(200, {"result": "APPLIED"})

    def opener(request: Any, *, timeout: float) -> FakeResponse:
        captured["data"] = request.data
        captured["headers"] = {name.casefold(): value for name, value in request.header_items()}
        captured["timeout"] = timeout
        return response

    result = simulator.send_request(artifact, timeout=3.25, opener=opener)

    assert result.status == 200
    assert captured["data"] is artifact.raw_body
    assert captured["timeout"] == 3.25
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["content-type"] == "application/json"
    assert headers["x-fulfillflow-event-id"] == artifact.event_id
    assert headers["x-fulfillflow-timestamp"] == SIGNED_TIMESTAMP
    assert "x-fulfillflow-signature" in headers
    assert response.closed is True


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_real_transport_rejects_cross_host_redirects_before_second_request(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _RedirectRecorder(status)
    target = _TargetRecorder()
    monkeypatch.setenv("CARRIER_ALPHA_WEBHOOK_SECRET", ALPHA_SECRET)

    with _serve(_target_handler(target)) as target_server:
        source.location = f"{_server_url(target_server)}/redirected?untrusted-location=%3Cscript%3E"
        with _serve(_redirect_handler(source)) as source_server:
            exit_code = simulator.main(_redirect_cli_arguments(_server_url(source_server)))

    captured = capsys.readouterr()
    signature = source.headers["x-fulfillflow-signature"]
    assert exit_code == 1
    assert source.request_count == 1
    assert target.request_count == 0
    assert source.headers["x-fulfillflow-event-id"] == "redirect-test-valid-01"
    assert source.headers["content-type"] == "application/json"
    assert source.raw_body
    assert '"error":"REDIRECT_REJECTED"' in captured.out
    assert "untrusted-location" not in captured.out
    assert "X-FulfillFlow" not in captured.out
    assert "Content-Type" not in captured.out
    assert signature not in captured.out
    assert ALPHA_SECRET not in captured.out
    assert source.raw_body.decode("utf-8") not in captured.out
    assert captured.err == ""


def test_real_transport_also_rejects_same_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = _RedirectRecorder(302)
    monkeypatch.setenv("CARRIER_ALPHA_WEBHOOK_SECRET", ALPHA_SECRET)

    with _serve(_redirect_handler(recorder)) as server:
        recorder.location = f"{_server_url(server)}/redirected"
        exit_code = simulator.main(_redirect_cli_arguments(_server_url(server)))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert recorder.request_count == 1
    assert recorder.redirected_request_count == 0
    assert '"error":"REDIRECT_REJECTED"' in captured.out
    assert recorder.location not in captured.out
    assert recorder.headers["x-fulfillflow-signature"] not in captured.out
    assert recorder.raw_body.decode("utf-8") not in captured.out
    assert captured.err == ""


def test_http_problem_response_is_parsed_without_treating_expected_422_as_transport_error() -> None:
    artifact = (
        simulator.build_scenario(
            _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_UNKNOWN_STATUS),
            signed_timestamp=SIGNED_TIMESTAMP,
        )
        .steps[0]
        .artifact
    )
    problem = io.BytesIO(b'{"code":"UNKNOWN_EXTERNAL_STATUS"}')

    def opener(request: Any, *, timeout: float) -> Any:
        del request, timeout
        raise HTTPError(artifact.url, 422, "Unprocessable Entity", {}, problem)

    result = simulator.send_request(artifact, timeout=1, opener=opener)

    assert result == simulator.HttpResult(422, {"code": "UNKNOWN_EXTERNAL_STATUS"})


def test_expected_negative_scenarios_are_successful_and_mismatches_fail() -> None:
    plan = simulator.build_scenario(
        _config(simulator.CARRIER_BETA, simulator.SCENARIO_UNKNOWN_STATUS),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    lines: list[str] = []

    assert simulator.run_scenario(
        plan,
        timeout=1,
        opener=lambda request, timeout: FakeResponse(422, {"code": "UNKNOWN_EXTERNAL_STATUS"}),
        emit=lines.append,
    )
    assert json.loads(lines[0]) == {
        "attempt": 1,
        "carrier": "carrier-beta",
        "event_id": plan.steps[0].artifact.event_id,
        "http_status": 422,
        "problem_code": "UNKNOWN_EXTERNAL_STATUS",
        "phase": "rejected",
        "scenario": "unknown-status",
        "step": "unknown-status",
        "success": True,
    }

    assert not simulator.run_scenario(
        plan,
        timeout=1,
        opener=lambda request, timeout: FakeResponse(401, {"code": "INVALID_WEBHOOK_SIGNATURE"}),
        emit=lambda line: None,
    )


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (TimeoutError(), "REQUEST_TIMEOUT"),
        (URLError(TimeoutError()), "REQUEST_TIMEOUT"),
        (URLError("connection refused"), "REQUEST_FAILED"),
        (OSError("network unavailable"), "REQUEST_FAILED"),
    ],
)
def test_timeout_and_network_errors_are_sanitized(error: Exception, expected_code: str) -> None:
    plan = simulator.build_scenario(
        _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_INVALID_SIGNATURE),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    output: list[str] = []

    def opener(request: Any, *, timeout: float) -> FakeResponse:
        del request, timeout
        raise error

    assert not simulator.run_scenario(plan, timeout=1, opener=opener, emit=output.append)
    observation = json.loads(output[0])
    assert observation["error"] == expected_code
    assert observation["success"] is False
    assert ALPHA_SECRET not in output[0]
    assert "sha256=" not in output[0]


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (TimeoutError(), simulator.SimulatorTimeoutError),
        (OSError("connection reset"), simulator.SimulatorConnectionError),
        (HTTPException("truncated response"), simulator.SimulatorResponseError),
    ],
)
def test_timeout_and_network_errors_while_reading_are_sanitized(
    error: Exception,
    expected_type: type[simulator.SimulatorError],
) -> None:
    artifact = (
        simulator.build_scenario(
            _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
            signed_timestamp=SIGNED_TIMESTAMP,
        )
        .steps[0]
        .artifact
    )

    class ReadFailureResponse(FakeResponse):
        def read(self, amount: int = -1) -> bytes:
            del amount
            raise error

    response = ReadFailureResponse(200, {})
    with pytest.raises(expected_type):
        simulator.send_request(
            artifact,
            timeout=1,
            opener=lambda request, timeout: response,
        )
    assert response.closed is True


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"\xff",
        b"{" + b'"padding":"' + b"x" * 65_536 + b'"}',
    ],
    ids=("not-json", "array", "invalid-utf8", "oversized"),
)
def test_invalid_or_oversized_response_fails_validation(body: bytes) -> None:
    artifact = (
        simulator.build_scenario(
            _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
            signed_timestamp=SIGNED_TIMESTAMP,
        )
        .steps[0]
        .artifact
    )

    class RawResponse(FakeResponse):
        def __init__(self) -> None:
            self.status = 200
            self.closed = False
            self._body = body

    with pytest.raises(simulator.SimulatorResponseError):
        simulator.send_request(artifact, timeout=1, opener=lambda request, timeout: RawResponse())


def test_carrier_selects_only_its_dedicated_secret_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARRIER_ALPHA_WEBHOOK_SECRET", ALPHA_SECRET)
    monkeypatch.setenv("CARRIER_BETA_WEBHOOK_SECRET", BETA_SECRET)
    monkeypatch.setenv("SECRET", "must-not-be-used")
    parser = simulator.build_parser()

    alpha = simulator._config_from_arguments(
        parser.parse_args(
            [
                "--carrier",
                "carrier-alpha",
                "--tracking-code",
                "alpha-1",
                "--scenario",
                "valid",
                "--seed",
                "7",
                "--start-at",
                "2026-08-31T15:00:00Z",
            ]
        )
    )
    beta = simulator._config_from_arguments(
        parser.parse_args(
            [
                "--carrier",
                "carrier-beta",
                "--tracking-code",
                "beta-1",
                "--scenario",
                "valid",
                "--seed",
                "7",
                "--start-at",
                "2026-08-31T15:00:00Z",
            ]
        )
    )

    assert alpha.secret == ALPHA_SECRET
    assert beta.secret == BETA_SECRET
    assert "must-not-be-used" not in repr(alpha)
    assert ALPHA_SECRET not in repr(alpha)
    assert BETA_SECRET not in repr(beta)


def test_cli_has_exact_scenarios_and_rejects_raw_secret_argument_without_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = simulator.build_parser()
    scenario_action = next(action for action in parser._actions if action.dest == "scenario")

    assert tuple(scenario_action.choices or ()) == (
        "valid",
        "duplicate",
        "out-of-order",
        "unknown-status",
        "invalid-signature",
    )
    with pytest.raises(SystemExit) as error:
        parser.parse_args(
            [
                "--carrier",
                "carrier-alpha",
                "--tracking-code",
                "alpha-1",
                "--scenario",
                "valid",
                "--secret",
                "forbidden-secret-value",
            ]
        )
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "forbidden-secret-value" not in stderr
    assert "invalid command-line arguments" in stderr


@pytest.mark.parametrize("carrier", ["", "not-a-carrier"])
def test_invalid_carrier_environment_is_sanitized_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    carrier: str,
) -> None:
    monkeypatch.setenv("FULFILLFLOW_CARRIER", carrier)

    with pytest.raises(SystemExit) as error:
        simulator.main(
            [
                "--tracking-code",
                "track-1",
                "--scenario",
                "valid",
            ]
        )

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    if carrier:
        assert carrier not in stderr
    assert "carrier must be carrier-alpha or carrier-beta" in stderr


def test_invalid_scenario_environment_is_sanitized_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FULFILLFLOW_CARRIER", simulator.CARRIER_ALPHA)
    monkeypatch.setenv("FULFILLFLOW_SCENARIO", "not-a-scenario")

    with pytest.raises(SystemExit) as error:
        simulator.main(["--tracking-code", "track-1"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "not-a-scenario" not in stderr
    assert "Traceback" not in stderr
    assert "documented scenarios" in stderr


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf"])
def test_timeout_must_be_finite_and_positive(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str,
) -> None:
    monkeypatch.setenv("CARRIER_ALPHA_WEBHOOK_SECRET", ALPHA_SECRET)
    parser = simulator.build_parser()
    arguments = parser.parse_args(
        [
            "--carrier",
            simulator.CARRIER_ALPHA,
            "--tracking-code",
            "track-1",
            "--scenario",
            "valid",
            f"--timeout={timeout}",
        ]
    )

    with pytest.raises(ValueError, match="timeout must be greater than zero"):
        simulator._config_from_arguments(arguments)


def test_start_time_overflow_is_sanitized_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CARRIER_ALPHA_WEBHOOK_SECRET", ALPHA_SECRET)

    with pytest.raises(SystemExit) as error:
        simulator.main(
            [
                "--carrier",
                simulator.CARRIER_ALPHA,
                "--tracking-code",
                "track-1",
                "--scenario",
                "valid",
                "--start-at",
                "9999-12-31T23:59:59Z",
            ]
        )

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "outside the supported scenario range" in stderr


def test_script_has_no_application_database_or_http_client_dependency() -> None:
    path = Path("scripts/simulate_carrier_events.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        (node.module or "").split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert "fulfillflow" not in imported_roots
    assert "sqlalchemy" not in imported_roots
    assert "psycopg" not in imported_roots
    assert "httpx" not in imported_roots
    assert "requests" not in imported_roots
    assert "DATABASE_URL" not in source


def test_request_artifact_rejects_duplicate_header_names() -> None:
    with pytest.raises(ValueError, match="headers must be unique"):
        simulator.RequestArtifact(
            url="http://127.0.0.1:8000/events",
            event_id="event-1",
            timestamp=SIGNED_TIMESTAMP,
            raw_body=b"{}",
            headers=(("X-Header", "one"), ("x-header", "two")),
        )


def test_event_id_timestamp_url_and_start_validation() -> None:
    with pytest.raises(ValueError, match="visible ASCII"):
        simulator.calculate_signature(
            ALPHA_SECRET,
            timestamp=SIGNED_TIMESTAMP,
            event_id="event with spaces",
            raw_body=b"{}",
        )
    with pytest.raises(ValueError, match="decimal ASCII"):
        simulator.calculate_signature(
            ALPHA_SECRET,
            timestamp="not-a-timestamp",
            event_id="event-1",
            raw_body=b"{}",
        )
    with pytest.raises(ValueError, match="credentials"):
        simulator._validate_base_url("http://user:password@localhost:8000")
    with pytest.raises(ValueError, match="valid host and port"):
        simulator._validate_base_url("http://127.0.0.1:notaport")
    with pytest.raises(ValueError, match="timezone"):
        simulator._parse_start("2026-08-31T15:00:00")


def _response_sequence(payloads: list[object]) -> Any:
    iterator: Iterator[object] = iter(payloads)

    def opener(request: Any, *, timeout: float) -> FakeResponse:
        del request, timeout
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        status, body = value
        return FakeResponse(status, body)

    return opener


def test_async_polling_uses_same_origin_without_forwarding_hmac_and_closes_responses():
    from uuid import uuid4

    plan = simulator.build_scenario(
        _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    identifier = str(uuid4())
    accepted = simulator.HttpResult(
        202, {"inbox_event_id": identifier}, f"/api/v1/carrier-events/{identifier}"
    )
    responses = [
        FakeResponse(200, {"id": identifier, "status": "RECEIVED"}),
        FakeResponse(
            200,
            {
                "id": identifier,
                "status": "PROCESSED",
                "result": {"result": "APPLIED", "current_status": "POSTED"},
            },
        ),
    ]
    pending = list(responses)
    clock = [0.0]

    def opener(request, *, timeout):
        assert request.full_url == f"http://127.0.0.1:8000{accepted.location}"
        assert request.method == "GET"
        assert request.header_items() == []
        assert 0 < timeout <= 2
        return pending.pop(0)

    def sleep(seconds):
        clock[0] += seconds

    result = simulator.observe_completion(
        plan.steps[0].artifact,
        accepted,
        timeout=2,
        completion_timeout=5,
        opener=opener,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    assert result.payload["result"] == "APPLIED"
    assert result.payload["external_event_id"] == plan.steps[0].artifact.event_id
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("failure", ["pending", "http", "network", "protocol"])
def test_observation_timeout_never_reports_business_rejection(failure):
    from uuid import uuid4

    identifier = str(uuid4())
    artifact = (
        simulator.build_scenario(
            _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
            signed_timestamp=SIGNED_TIMESTAMP,
        )
        .steps[0]
        .artifact
    )
    accepted = simulator.HttpResult(
        202, {"inbox_event_id": identifier}, f"/api/v1/carrier-events/{identifier}"
    )
    clock = [0.0]
    calls = []
    errors = []
    observations = []

    def opener(request, *, timeout):
        calls.append(timeout)
        if failure == "protocol":
            raise HTTPException("private wire details")
        if failure == "network":
            raise URLError("unavailable")
        if failure == "http":
            body = io.BytesIO(b"{}")
            errors.append(body)
            raise HTTPError(request.full_url, 503, "unavailable", {}, body)
        return FakeResponse(200, {"id": identifier, "status": "RECEIVED"})

    def sleep(seconds):
        clock[0] += seconds

    with pytest.raises(simulator.SimulatorObservationTimeoutError) as error:
        simulator.observe_completion(
            artifact,
            accepted,
            timeout=2,
            completion_timeout=3,
            emit=observations.append,
            opener=opener,
            monotonic=lambda: clock[0],
            sleep=sleep,
        )
    assert error.value.code == "RESULT_NOT_OBSERVED"
    assert len(calls) == 3
    assert all(body.closed for body in errors)
    assert accepted.status == 202
    assert len(observations) == (0 if failure == "pending" else 3)
    assert all(json.loads(line)["phase"] == "observation_failed" for line in observations)
    assert "private wire details" not in str(observations)


@pytest.mark.parametrize(
    "location",
    ["https://evil.test/result", "/api/v1/carrier-events/wrong", "//evil.test/result", None],
)
def test_acceptance_location_must_match_original_inbox(location):
    from uuid import uuid4

    artifact = (
        simulator.build_scenario(
            _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
            signed_timestamp=SIGNED_TIMESTAMP,
        )
        .steps[0]
        .artifact
    )
    accepted = simulator.HttpResult(202, {"inbox_event_id": str(uuid4())}, location)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid Location must not cause a request")

    with pytest.raises(simulator.SimulatorResponseError):
        simulator.observe_completion(
            artifact, accepted, timeout=1, completion_timeout=1, opener=forbidden
        )


def test_async_simulator_emits_acceptance_separately_from_completion():
    from uuid import uuid4

    plan = simulator.build_scenario(
        _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    output = []
    pending = {}

    def opener(request, *, timeout):
        if request.method == "POST":
            step = plan.steps[len(pending)]
            identifier = str(uuid4())
            location = f"/api/v1/carrier-events/{identifier}"
            pending[location] = (identifier, step)
            response = FakeResponse(202, {"inbox_event_id": identifier})
            response.headers = {"Location": location}
            return response
        identifier, step = pending[request.selector]
        return FakeResponse(
            200,
            {
                "id": identifier,
                "status": "PROCESSED",
                "result": {
                    "result": step.expected.result,
                    "previous_status": step.expected.previous_status,
                    "current_status": step.expected.current_status,
                },
            },
        )

    assert simulator.run_scenario(plan, timeout=1, opener=opener, emit=output.append)
    records = [json.loads(line) for line in output]
    assert len(records) == 8
    assert [item["http_status"] for item in records] == [202, 200] * 4
    assert all(records[i]["phase"] == "accepted" for i in range(0, 8, 2))
    assert all(records[i]["success"] for i in range(1, 8, 2))


@pytest.mark.parametrize("terminal", ["expired", "rejected"])
def test_accepted_failure_never_resubmits_admission(monkeypatch, terminal):
    from uuid import uuid4

    plan = simulator.build_scenario(
        _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    identifier = str(uuid4())
    calls = []
    output = []

    def send(artifact, **kwargs):
        calls.append(artifact)
        return simulator.HttpResult(
            202, {"inbox_event_id": identifier}, f"/api/v1/carrier-events/{identifier}"
        )

    def observe(*args, **kwargs):
        if terminal == "expired":
            raise simulator.SimulatorObservationTimeoutError("unobserved")
        return simulator.HttpResult(200, {"kind": "rejected", "code": "SHIPMENT_NOT_FOUND"})

    monkeypatch.setattr(simulator, "send_request", send)
    monkeypatch.setattr(simulator, "observe_completion", observe)
    assert not simulator.run_scenario(plan, timeout=1, emit=output.append)
    assert len(calls) == 1
    records = [json.loads(line) for line in output]
    assert records[0]["phase"] == "accepted"
    assert records[1]["phase"] == ("observation_expired" if terminal == "expired" else "rejected")


@pytest.mark.parametrize("status,phase", [(503, "failed"), (202, "accepted")])
def test_unexpected_admission_response_is_not_business_completion_or_rejection(status, phase):
    plan = simulator.build_scenario(
        _config(simulator.CARRIER_ALPHA, simulator.SCENARIO_VALID),
        signed_timestamp=SIGNED_TIMESTAMP,
    )
    output = []
    assert not simulator.run_scenario(
        plan,
        timeout=1,
        mode="synchronous",
        opener=lambda request, timeout: FakeResponse(status, {}),
        emit=output.append,
    )
    assert json.loads(output[0])["phase"] == phase

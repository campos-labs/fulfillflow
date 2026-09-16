"""Send deterministic synthetic Carrier events through FulfillFlow's public webhook.

This script is intentionally an external consumer.  It uses only the Python
standard library, reads the selected Carrier's secret from its dedicated
environment variable, and never imports application code.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import random
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
from typing import Any, Never
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

CARRIER_ALPHA = "carrier-alpha"
CARRIER_BETA = "carrier-beta"
CARRIERS = (CARRIER_ALPHA, CARRIER_BETA)

SCENARIO_VALID = "valid"
SCENARIO_DUPLICATE = "duplicate"
SCENARIO_OUT_OF_ORDER = "out-of-order"
SCENARIO_UNKNOWN_STATUS = "unknown-status"
SCENARIO_INVALID_SIGNATURE = "invalid-signature"
SCENARIOS = (
    SCENARIO_VALID,
    SCENARIO_DUPLICATE,
    SCENARIO_OUT_OF_ORDER,
    SCENARIO_UNKNOWN_STATUS,
    SCENARIO_INVALID_SIGNATURE,
)

SECRET_ENV_BY_CARRIER = {
    CARRIER_ALPHA: "CARRIER_ALPHA_WEBHOOK_SECRET",
    CARRIER_BETA: "CARRIER_BETA_WEBHOOK_SECRET",
}

_MAX_EVENT_ID_LENGTH = 128
_MAX_TRACKING_CODE_LENGTH = 80
_MAX_RESPONSE_BYTES = 65_536
_VISIBLE_ASCII_START = 0x21
_VISIBLE_ASCII_END = 0x7E


class SimulatorError(RuntimeError):
    """A sanitized simulator failure suitable for normal CLI output."""

    code = "SIMULATOR_ERROR"


class SimulatorTimeoutError(SimulatorError):
    """The public webhook did not answer before the configured timeout."""

    code = "REQUEST_TIMEOUT"


class SimulatorConnectionError(SimulatorError):
    """The public webhook could not be reached."""

    code = "REQUEST_FAILED"


class SimulatorResponseError(SimulatorError):
    """The webhook returned a response that could not be safely interpreted."""

    code = "INVALID_RESPONSE"


class SimulatorRedirectError(SimulatorError):
    """The webhook attempted a redirect that the external client refuses."""

    code = "REDIRECT_REJECTED"


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Reject every HTTP redirect before urllib can construct another request."""

    def http_error_301(
        self,
        request: Any,
        response: Any,
        code: int,
        message: str,
        headers: Any,
    ) -> Never:
        del request, code, message, headers
        _close_response(response)
        raise SimulatorRedirectError("Webhook redirects are not allowed.")

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


class SimulatorArgumentParser(argparse.ArgumentParser):
    """Argument parser that never reflects rejected command-line values."""

    def error(self, message: str) -> Never:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid command-line arguments.\n")

    def configuration_error(self, message: str) -> Never:
        """Report an internally generated, value-free validation message."""
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


@dataclass(frozen=True, slots=True)
class SimulatorConfig:
    """Validated command-line configuration."""

    base_url: str
    carrier: str
    tracking_code: str
    scenario: str
    seed: int | None
    prefix: str
    start: datetime
    timeout: float
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RequestArtifact:
    """The exact request bytes and authentication artifacts sent on the wire."""

    url: str
    event_id: str
    timestamp: str
    raw_body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)

    def __post_init__(self) -> None:
        names = [name.casefold() for name, _ in self.headers]
        if len(names) != len(set(names)):
            raise ValueError("request headers must be unique")


@dataclass(frozen=True, slots=True)
class ExpectedResponse:
    """Exact observable outcome required for one scenario step."""

    status: int
    result: str | None = None
    problem_code: str | None = None
    original_result: str | None = None
    previous_status: str | None = None
    current_status: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """One request and the public result that makes it successful."""

    name: str
    artifact: RequestArtifact
    expected: ExpectedResponse


@dataclass(frozen=True, slots=True)
class ScenarioPlan:
    """Complete deterministic sequence for one documented simulator scenario."""

    carrier: str
    tracking_code: str
    scenario: str
    steps: tuple[ScenarioStep, ...]


@dataclass(frozen=True, slots=True)
class HttpResult:
    """A bounded JSON response from the public webhook."""

    status: int
    payload: Mapping[str, object]
    location: str | None = None


def calculate_signature(
    secret: str,
    *,
    timestamp: str,
    event_id: str,
    raw_body: bytes,
) -> str:
    """Calculate the public HMAC-SHA256 protocol without application imports."""
    _validate_event_id(event_id)
    if not timestamp.isascii() or not timestamp.isdecimal():
        raise ValueError("timestamp must contain decimal ASCII digits")
    signed_payload = timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def serialize_payload(payload: Mapping[str, object]) -> bytes:
    """Serialize a payload once to the exact UTF-8 bytes that will be signed."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def build_scenario(
    config: SimulatorConfig,
    *,
    signed_timestamp: str | None = None,
) -> ScenarioPlan:
    """Build one of the five documented scenarios for Alpha or Beta."""
    timestamp = signed_timestamp or str(int(time.time()))
    if not timestamp.isascii() or not timestamp.isdecimal():
        raise ValueError("signed timestamp must contain decimal ASCII digits")

    if config.scenario == SCENARIO_VALID:
        return _valid_plan(config, timestamp)
    if config.scenario == SCENARIO_DUPLICATE:
        return _duplicate_plan(config, timestamp)
    if config.scenario == SCENARIO_OUT_OF_ORDER:
        return _out_of_order_plan(config, timestamp)
    if config.scenario == SCENARIO_UNKNOWN_STATUS:
        return _unknown_status_plan(config, timestamp)
    if config.scenario == SCENARIO_INVALID_SIGNATURE:
        return _invalid_signature_plan(config, timestamp)
    raise ValueError(f"unsupported scenario: {config.scenario}")


def send_request(
    artifact: RequestArtifact,
    *,
    timeout: float,
    opener: Callable[..., Any] | None = None,
) -> HttpResult:
    """Send exactly one artifact and return a bounded, parsed HTTP response."""
    try:
        request = Request(
            artifact.url,
            data=artifact.raw_body,
            headers=dict(artifact.headers),
            method="POST",
        )
    except (TypeError, ValueError) as error:
        raise SimulatorConnectionError("Webhook endpoint could not be reached.") from error
    open_request = opener or _open_without_redirects
    try:
        response = open_request(request, timeout=timeout)
    except HTTPError as error:
        try:
            return _read_http_result(error, status=error.code)
        finally:
            _close_response(error)
    except TimeoutError as error:
        raise SimulatorTimeoutError("Webhook request timed out.") from error
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise SimulatorTimeoutError("Webhook request timed out.") from error
        raise SimulatorConnectionError("Webhook endpoint could not be reached.") from error
    except OSError as error:
        raise SimulatorConnectionError("Webhook endpoint could not be reached.") from error
    except (HTTPException, ValueError) as error:
        raise SimulatorConnectionError("Webhook endpoint could not be reached.") from error

    try:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        return _read_http_result(response, status=status)
    finally:
        _close_response(response)


class SimulatorObservationTimeoutError(SimulatorError):
    """Accepted work has no terminal result observed before the observation deadline."""

    code = "RESULT_NOT_OBSERVED"


def observe_completion(
    artifact: RequestArtifact,
    accepted: HttpResult,
    *,
    timeout: float,
    completion_timeout: float,
    opener: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] | None = None,
) -> HttpResult:
    try:
        identifier = str(UUID(str(accepted.payload["inbox_event_id"])))
    except (KeyError, ValueError) as error:
        raise SimulatorResponseError("Invalid acceptance identity.") from error
    expected = f"/api/v1/carrier-events/{identifier}"
    if accepted.location != expected:
        raise SimulatorResponseError("Invalid acceptance Location.")
    origin = urlsplit(artifact.url)
    url = f"{origin.scheme}://{origin.netloc}{expected}"
    deadline = monotonic() + completion_timeout
    open_request = opener or _open_without_redirects
    while monotonic() < deadline:
        response = None
        try:
            response = open_request(
                Request(url, method="GET"), timeout=min(timeout, max(0.001, deadline - monotonic()))
            )
            result = _read_http_result(response, status=response.status)
            payload = result.payload
            if str(payload.get("id")) != identifier:
                raise SimulatorResponseError("Polling returned a different inbox identity.")
            if payload.get("status") in ("PROCESSED", "REJECTED"):
                decision = payload.get("result")
                if not isinstance(decision, dict):
                    raise SimulatorResponseError("Terminal result is missing.")
                return HttpResult(
                    result.status, dict(decision, external_event_id=artifact.event_id)
                )
        except HTTPError as error:
            _close_response(error)
            if emit is not None:
                emit(_json_line({"phase": "observation_failed", "inbox_event_id": identifier}))
        except (URLError, TimeoutError, OSError, HTTPException):
            # A failed observation never changes the accepted business state.
            if emit is not None:
                emit(_json_line({"phase": "observation_failed", "inbox_event_id": identifier}))
        finally:
            if response is not None:
                _close_response(response)
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(1.0, remaining))
    raise SimulatorObservationTimeoutError("Accepted; terminal result not yet observed.")


def _open_without_redirects(request: Request, *, timeout: float) -> Any:
    """Use an explicit opener whose redirect handler never issues a second request."""
    return build_opener(_RejectRedirectHandler()).open(request, timeout=timeout)


def run_scenario(
    plan: ScenarioPlan,
    *,
    timeout: float,
    opener: Callable[..., Any] | None = None,
    emit: Callable[[str], None] = print,
    mode: str = "async",
    completion_timeout: float = 30.0,
) -> bool:
    """Execute a plan, stopping safely at the first unexpected result."""
    for position, step in enumerate(plan.steps, start=1):
        try:
            response = send_request(step.artifact, timeout=timeout, opener=opener)
            if response.status == 202 and mode == "async":
                emit(
                    _json_line(
                        {
                            "phase": "accepted",
                            "http_status": 202,
                            "event_id": step.artifact.event_id,
                            "inbox_event_id": response.payload.get("inbox_event_id"),
                        }
                    )
                )
                response = observe_completion(
                    step.artifact,
                    response,
                    timeout=timeout,
                    completion_timeout=completion_timeout,
                    opener=opener,
                    emit=emit,
                )
        except SimulatorError as error:
            emit(
                _json_line(
                    {
                        "carrier": plan.carrier,
                        "scenario": plan.scenario,
                        "step": step.name,
                        "attempt": position,
                        "event_id": step.artifact.event_id,
                        "success": False,
                        "error": error.code,
                        "phase": (
                            "observation_expired"
                            if isinstance(error, SimulatorObservationTimeoutError)
                            else "failed"
                        ),
                    }
                )
            )
            return False

        matched = _matches_expected(step, response)
        observation: dict[str, object] = {
            "carrier": plan.carrier,
            "scenario": plan.scenario,
            "step": step.name,
            "attempt": position,
            "event_id": step.artifact.event_id,
            "http_status": response.status,
            "phase": (
                "accepted"
                if response.status == 202
                else "failed"
                if response.status >= 500
                else "rejected"
                if response.payload.get("kind") == "rejected" or response.status >= 400
                else "completed"
            ),
            "success": matched,
        }
        observation.update(_safe_operational_result(response.payload))
        emit(_json_line(observation))
        if not matched:
            return False
    return True


def build_parser() -> SimulatorArgumentParser:
    """Create the dependency-free CLI parser."""
    parser = SimulatorArgumentParser(
        description=(
            "Send one documented synthetic Carrier scenario to FulfillFlow's public webhook."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FULFILLFLOW_BASE_URL", "http://127.0.0.1:8000"),
        help="Application base URL (default: %(default)s).",
    )
    parser.add_argument(
        "--carrier",
        choices=CARRIERS,
        default=os.environ.get("FULFILLFLOW_CARRIER"),
        required=os.environ.get("FULFILLFLOW_CARRIER") is None,
    )
    parser.add_argument(
        "--tracking-code",
        "--tracking",
        dest="tracking_code",
        default=os.environ.get("FULFILLFLOW_TRACKING_CODE"),
        required=os.environ.get("FULFILLFLOW_TRACKING_CODE") is None,
        help="Tracking code of an existing Shipment.",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default=os.environ.get("FULFILLFLOW_SCENARIO"),
        required=os.environ.get("FULFILLFLOW_SCENARIO") is None,
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--event-id-prefix",
        "--prefix",
        dest="prefix",
        default=None,
        help="Visible-ASCII event ID prefix; generated uniquely when omitted.",
    )
    parser.add_argument(
        "--start-at",
        "--start",
        dest="start",
        default=None,
        help="Timezone-aware ISO 8601 occurrence time for the first event.",
    )
    parser.add_argument(
        "--timeout-seconds",
        "--timeout",
        dest="timeout",
        type=float,
        default=10.0,
        help="Per-request HTTP timeout in seconds (default: %(default)s).",
    )
    parser.add_argument("--mode", choices=("async", "synchronous"), default="async")
    parser.add_argument("--completion-timeout-seconds", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; zero means the scenario matched its exact contract."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if (
            not math.isfinite(arguments.completion_timeout_seconds)
            or arguments.completion_timeout_seconds <= 0
        ):
            raise ValueError("completion timeout must be greater than zero")
        config = _config_from_arguments(arguments)
        plan = build_scenario(config)
    except OverflowError:
        parser.configuration_error("start time is outside the supported scenario range")
    except ValueError as error:
        parser.configuration_error(str(error))
    return (
        0
        if run_scenario(
            plan,
            timeout=config.timeout,
            mode=arguments.mode,
            completion_timeout=arguments.completion_timeout_seconds,
        )
        else 1
    )


def _config_from_arguments(arguments: argparse.Namespace) -> SimulatorConfig:
    carrier = str(arguments.carrier)
    if carrier not in CARRIERS:
        raise ValueError("carrier must be carrier-alpha or carrier-beta")
    scenario = str(arguments.scenario)
    if scenario not in SCENARIOS:
        raise ValueError("scenario must be one of the documented scenarios")
    secret_env = SECRET_ENV_BY_CARRIER[carrier]
    secret = os.environ.get(secret_env)
    if secret is None or not secret:
        raise ValueError(f"{secret_env} must be set for {carrier}")

    base_url = _validate_base_url(str(arguments.base_url))
    tracking_code = str(arguments.tracking_code).strip().upper()
    if not 1 <= len(tracking_code) <= _MAX_TRACKING_CODE_LENGTH:
        raise ValueError("tracking code must contain between 1 and 80 characters")
    timeout = float(arguments.timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    start = _parse_start(arguments.start)
    prefix = _resolve_prefix(carrier, arguments.seed, arguments.prefix)
    return SimulatorConfig(
        base_url=base_url,
        carrier=carrier,
        tracking_code=tracking_code,
        scenario=scenario,
        seed=arguments.seed,
        prefix=prefix,
        start=start,
        timeout=timeout,
        secret=secret,
    )


def _valid_plan(config: SimulatorConfig, timestamp: str) -> ScenarioPlan:
    external_statuses = (
        ("CREATED", "POSTED"),
        ("MOVING", "IN_TRANSIT"),
        ("OUT_FOR_DELIVERY", "OUT_FOR_DELIVERY"),
        ("DELIVERED", "DELIVERED"),
    )
    if config.carrier == CARRIER_BETA:
        external_statuses = (
            ("label_created", "POSTED"),
            ("hub_scan", "IN_TRANSIT"),
            ("courier_route", "OUT_FOR_DELIVERY"),
            ("completed", "DELIVERED"),
        )

    previous = "PENDING"
    steps: list[ScenarioStep] = []
    for index, (external, current) in enumerate(external_statuses, start=1):
        event_id = _event_id(config.prefix, f"valid-{index:02d}")
        artifact = _artifact(
            config,
            timestamp=timestamp,
            event_id=event_id,
            external_status=external,
            occurred_at=config.start + timedelta(minutes=index - 1),
        )
        steps.append(
            ScenarioStep(
                name=f"valid-{index:02d}",
                artifact=artifact,
                expected=ExpectedResponse(
                    status=200,
                    result="APPLIED",
                    previous_status=previous,
                    current_status=current,
                ),
            )
        )
        previous = current
    return ScenarioPlan(config.carrier, config.tracking_code, config.scenario, tuple(steps))


def _duplicate_plan(config: SimulatorConfig, timestamp: str) -> ScenarioPlan:
    event_id = _event_id(config.prefix, "duplicate-01")
    artifact = _artifact(
        config,
        timestamp=timestamp,
        event_id=event_id,
        external_status=_external_status(config.carrier, "POSTED"),
        occurred_at=config.start,
    )
    first = ScenarioStep(
        "original",
        artifact,
        ExpectedResponse(
            status=200,
            result="APPLIED",
            previous_status="PENDING",
            current_status="POSTED",
        ),
    )
    repeated = ScenarioStep(
        "duplicate",
        artifact,
        ExpectedResponse(
            status=200,
            result="DUPLICATE",
            original_result="APPLIED",
            previous_status="PENDING",
            current_status="POSTED",
        ),
    )
    return ScenarioPlan(config.carrier, config.tracking_code, config.scenario, (first, repeated))


def _out_of_order_plan(config: SimulatorConfig, timestamp: str) -> ScenarioPlan:
    newer = _artifact(
        config,
        timestamp=timestamp,
        event_id=_event_id(config.prefix, "out-of-order-newer"),
        external_status=_external_status(config.carrier, "IN_TRANSIT"),
        occurred_at=config.start + timedelta(minutes=1),
    )
    older = _artifact(
        config,
        timestamp=timestamp,
        event_id=_event_id(config.prefix, "out-of-order-older"),
        external_status=_external_status(config.carrier, "POSTED"),
        occurred_at=config.start,
    )
    return ScenarioPlan(
        config.carrier,
        config.tracking_code,
        config.scenario,
        (
            ScenarioStep(
                "newer-first",
                newer,
                ExpectedResponse(
                    status=200,
                    result="APPLIED",
                    previous_status="PENDING",
                    current_status="IN_TRANSIT",
                ),
            ),
            ScenarioStep(
                "older-second",
                older,
                ExpectedResponse(
                    status=200,
                    result="IGNORED_STALE",
                    previous_status="IN_TRANSIT",
                    current_status="IN_TRANSIT",
                ),
            ),
        ),
    )


def _unknown_status_plan(config: SimulatorConfig, timestamp: str) -> ScenarioPlan:
    artifact = _artifact(
        config,
        timestamp=timestamp,
        event_id=_event_id(config.prefix, "unknown-status-01"),
        external_status="SIMULATOR_UNKNOWN_STATUS",
        occurred_at=config.start,
    )
    return ScenarioPlan(
        config.carrier,
        config.tracking_code,
        config.scenario,
        (
            ScenarioStep(
                "unknown-status",
                artifact,
                ExpectedResponse(status=422, problem_code="UNKNOWN_EXTERNAL_STATUS"),
            ),
        ),
    )


def _invalid_signature_plan(config: SimulatorConfig, timestamp: str) -> ScenarioPlan:
    artifact = _artifact(
        config,
        timestamp=timestamp,
        event_id=_event_id(config.prefix, "invalid-signature-01"),
        external_status=_external_status(config.carrier, "POSTED"),
        occurred_at=config.start,
        invalidate_signature=True,
    )
    return ScenarioPlan(
        config.carrier,
        config.tracking_code,
        config.scenario,
        (
            ScenarioStep(
                "invalid-signature",
                artifact,
                ExpectedResponse(status=401, problem_code="INVALID_WEBHOOK_SIGNATURE"),
            ),
        ),
    )


def _artifact(
    config: SimulatorConfig,
    *,
    timestamp: str,
    event_id: str,
    external_status: str,
    occurred_at: datetime,
    invalidate_signature: bool = False,
) -> RequestArtifact:
    payload = _payload(
        config.carrier,
        event_id=event_id,
        tracking_code=config.tracking_code,
        external_status=external_status,
        occurred_at=occurred_at,
        seed=config.seed,
    )
    raw_body = serialize_payload(payload)
    signature = calculate_signature(
        config.secret,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    if invalidate_signature:
        signature = _corrupt_signature(signature)
    headers = (
        ("Content-Type", "application/json"),
        ("X-FulfillFlow-Event-Id", event_id),
        ("X-FulfillFlow-Timestamp", timestamp),
        ("X-FulfillFlow-Signature", signature),
    )
    return RequestArtifact(
        url=f"{config.base_url}/api/v1/carriers/{config.carrier}/events",
        event_id=event_id,
        timestamp=timestamp,
        raw_body=raw_body,
        headers=headers,
    )


def _payload(
    carrier: str,
    *,
    event_id: str,
    tracking_code: str,
    external_status: str,
    occurred_at: datetime,
    seed: int | None,
) -> dict[str, object]:
    location_index = random.Random(seed).randrange(2) if seed is not None else 0
    if carrier == CARRIER_ALPHA:
        cities = ("São Bernardo do Campo", "Campinas")
        return {
            "eventId": event_id,
            "trackingCode": tracking_code,
            "status": external_status,
            "eventDate": _iso8601(occurred_at),
            "city": cities[location_index],
            "description": "Synthetic Alpha carrier event",
        }

    locations = (("São Paulo", "SP"), ("Curitiba", "PR"))
    city, state = locations[location_index]
    return {
        "id": event_id,
        "tracking_number": tracking_code,
        "event": {
            "type": external_status,
            "occurred_at": _iso8601(occurred_at),
            "details": "Synthetic Beta carrier event",
        },
        "location": {"city": city, "state": state},
    }


def _external_status(carrier: str, canonical_status: str) -> str:
    alpha = {"POSTED": "CREATED", "IN_TRANSIT": "MOVING"}
    beta = {"POSTED": "label_created", "IN_TRANSIT": "hub_scan"}
    return (alpha if carrier == CARRIER_ALPHA else beta)[canonical_status]


def _matches_expected(step: ScenarioStep, response: HttpResult) -> bool:
    expected = step.expected
    if response.status != expected.status:
        return False
    if expected.result is not None:
        if response.payload.get("result") != expected.result:
            return False
        if response.payload.get("external_event_id") != step.artifact.event_id:
            return False
    if expected.problem_code is not None and response.payload.get("code") != expected.problem_code:
        return False
    optional_fields = {
        "original_result": expected.original_result,
        "previous_status": expected.previous_status,
        "current_status": expected.current_status,
    }
    return all(
        expected_value is None or response.payload.get(name) == expected_value
        for name, expected_value in optional_fields.items()
    )


def _safe_operational_result(payload: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for source, destination in (
        ("result", "result"),
        ("original_result", "original_result"),
        ("current_status", "current_status"),
        ("previous_status", "previous_status"),
        ("code", "problem_code"),
    ):
        value = payload.get(source)
        if source in payload and value is None and source == "previous_status":
            safe[destination] = None
        elif isinstance(value, str) and _safe_token(value):
            safe[destination] = value
    return safe


def _read_json_response(response: Any) -> Mapping[str, object]:
    raw_body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw_body) > _MAX_RESPONSE_BYTES:
        raise SimulatorResponseError("Webhook response exceeded the safe display limit.")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SimulatorResponseError("Webhook response was not valid UTF-8 JSON.") from error
    if not isinstance(payload, dict):
        raise SimulatorResponseError("Webhook response must be a JSON object.")
    return payload


def _read_http_result(response: Any, *, status: object) -> HttpResult:
    try:
        resolved_status = int(status)
        payload = _read_json_response(response)
    except SimulatorError:
        raise
    except TimeoutError as error:
        raise SimulatorTimeoutError("Webhook request timed out.") from error
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise SimulatorTimeoutError("Webhook request timed out.") from error
        raise SimulatorConnectionError("Webhook endpoint could not be reached.") from error
    except HTTPException as error:
        raise SimulatorResponseError("Webhook response could not be read.") from error
    except OSError as error:
        raise SimulatorConnectionError("Webhook endpoint could not be reached.") from error
    except (TypeError, ValueError) as error:
        raise SimulatorResponseError("Webhook response status was invalid.") from error
    headers = getattr(response, "headers", {})
    return HttpResult(resolved_status, payload, headers.get("Location"))


def _close_response(response: Any) -> None:
    try:
        response.close()
    except (HTTPException, OSError):
        pass


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.isascii() or any(character.isspace() for character in normalized):
        raise ValueError("base URL must be an absolute HTTP or HTTPS URL")
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError as error:
        raise ValueError("base URL must contain a valid host and port") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise ValueError("base URL must be an absolute HTTP or HTTPS URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("base URL must not contain credentials, query, or fragment")
    return normalized


def _parse_start(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("start must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("start must include a timezone")
    return parsed.replace(microsecond=0)


def _resolve_prefix(carrier: str, seed: int | None, explicit: str | None) -> str:
    if explicit is not None:
        prefix = explicit
    elif seed is not None:
        token = random.Random(seed).getrandbits(48)
        prefix = f"sim-{carrier.removeprefix('carrier-')}-{token:012x}"
    else:
        prefix = f"sim-{carrier.removeprefix('carrier-')}-{uuid4().hex[:12]}"
    _validate_visible_ascii(prefix, name="event ID prefix")
    return prefix


def _event_id(prefix: str, suffix: str) -> str:
    event_id = f"{prefix}-{suffix}"
    _validate_event_id(event_id)
    return event_id


def _validate_event_id(event_id: str) -> None:
    if not 1 <= len(event_id) <= _MAX_EVENT_ID_LENGTH:
        raise ValueError("event ID must contain between 1 and 128 characters")
    _validate_visible_ascii(event_id, name="event ID")


def _validate_visible_ascii(value: str, *, name: str) -> None:
    if not value or any(
        not _VISIBLE_ASCII_START <= ord(character) <= _VISIBLE_ASCII_END for character in value
    ):
        raise ValueError(f"{name} must contain visible ASCII characters only")


def _iso8601(value: datetime) -> str:
    rendered = value.isoformat(timespec="seconds")
    return f"{rendered[:-6]}Z" if rendered.endswith("+00:00") else rendered


def _corrupt_signature(signature: str) -> str:
    digest = signature.removeprefix("sha256=")
    replacement = "0" if digest[0] != "0" else "1"
    return f"sha256={replacement}{digest[1:]}"


def _safe_token(value: str) -> bool:
    return 1 <= len(value) <= 64 and all(
        character.isascii() and (character.isalnum() or character in {"_", "-"})
        for character in value
    )


def _json_line(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())

"""HTTP-only Locust contract for reproducible FulfillFlow benchmark campaigns."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import gevent  # type: ignore[import-untyped]
from locust import HttpUser, between, events, task
from locust import stats as locust_stats
from locust.env import Environment

from benchmarks.campaign import (
    CampaignBundle,
    CohortSlot,
    LoadLevel,
    balanced_active_slots,
    canonical_payload_bytes,
    carrier_payload,
    cycle_target_status,
    deterministic_event_id,
    load_campaign,
)

RequestKind = Literal["shipment_detail", "timeline", "webhook", "shipment_list"]
Phase = Literal["barrier", "warmup", "measurement", "draining", "finished", "invalid"]
ProcessPhase = Literal["warmup", "measurement"]

MIXED_REQUEST_PLAN = cast(
    tuple[RequestKind, ...],
    ("shipment_detail",) * 25 + ("timeline",) * 25 + ("webhook",) * 30 + ("shipment_list",) * 20,
)
TIMELINE_REQUEST_PLAN = cast(
    tuple[RequestKind, ...],
    ("shipment_detail",) * 50 + ("timeline",) * 50,
)
INGESTION_REQUEST_PLAN = cast(tuple[RequestKind, ...], ("webhook",))


def effective_request_weights(plan: tuple[RequestKind, ...]) -> dict[str, int]:
    """Return percentage weights derived from executable request slots."""
    if not plan:
        raise ValueError("request plan cannot be empty")
    counts = Counter(plan)
    return {key: (value * 100) // len(plan) for key, value in sorted(counts.items())}


def request_plan(profile: str) -> tuple[RequestKind, ...]:
    """Resolve a frozen profile without importing application code."""
    if profile == "mixed":
        return MIXED_REQUEST_PLAN
    if profile == "timeline":
        return TIMELINE_REQUEST_PLAN
    if profile == "ingestion":
        return INGESTION_REQUEST_PLAN
    raise ValueError(f"unsupported benchmark profile: {profile}")


@dataclass(frozen=True, slots=True)
class UserAssignment:
    index: int
    warmup: CohortSlot
    measurement: CohortSlot
    timeline_shipment_id: str


class CampaignRuntime:
    """Coordinate exclusive ownership and deterministic phase boundaries."""

    def __init__(
        self,
        bundle: CampaignBundle,
        load: LoadLevel,
        process_phase: ProcessPhase,
    ) -> None:
        self.bundle = bundle
        self.load = load
        self.process_phase = process_phase
        if effective_request_weights(request_plan(bundle.manifest.profile)) != {
            key: value for key, value in bundle.manifest.weights.model_dump().items() if value > 0
        }:
            raise ValueError("executable request plan diverges from the campaign weights")
        self._warmup_slots = balanced_active_slots(bundle.warmup, load.users)
        self._measurement_slots = balanced_active_slots(bundle.measurement, load.users)
        self.phase: Phase = "barrier"
        self.phase_started_at: float | None = None
        self.invalid_reason: str | None = None
        self._lock = threading.RLock()
        self._registered = 0
        self._warmup_completed: dict[int, int] = {}
        self._in_flight = 0

    def register_user(self) -> UserAssignment:
        """Allocate one disjoint warm-up/measurement pair exactly once."""
        with self._lock:
            index = self._registered
            if index >= self.load.users:
                raise RuntimeError("more Locust users started than declared by the load level")
            self._registered += 1
            self._warmup_completed[index] = 0
        timeline = self.bundle.timeline[index % len(self.bundle.timeline)]
        return UserAssignment(
            index=index,
            warmup=self._warmup_slots[index],
            measurement=self._measurement_slots[index],
            timeline_shipment_id=str(timeline.shipment_id),
        )

    @property
    def all_users_ready(self) -> bool:
        with self._lock:
            return self._registered == self.load.users

    def begin_phase(self, phase: Literal["warmup", "measurement"]) -> None:
        with self._lock:
            self.phase = phase
            self.phase_started_at = time.monotonic()

    def begin_request(self) -> bool:
        with self._lock:
            if self.phase not in {"warmup", "measurement"}:
                return False
            self._in_flight += 1
            return True

    def finish_request(self) -> None:
        with self._lock:
            self._in_flight -= 1
            if self._in_flight < 0:
                raise AssertionError("request in-flight counter became negative")

    def record_warmup_applied(self, user_index: int) -> None:
        with self._lock:
            self._warmup_completed[user_index] += 1

    def warmup_completed_for(self, user_index: int) -> int:
        with self._lock:
            return self._warmup_completed[user_index]

    @property
    def warmup_complete(self) -> bool:
        quota = self.bundle.manifest.warmup_quota_per_shipment
        with self._lock:
            return self._registered == self.load.users and all(
                count == quota for count in self._warmup_completed.values()
            )

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def invalidate(self, reason: str) -> None:
        """Stop the mutable sequence after the first ambiguous result."""
        with self._lock:
            if self.invalid_reason is None:
                self.invalid_reason = reason
            self.phase = "invalid"

    def stop_new_requests(self) -> None:
        """Atomically close admission while allowing accepted requests to drain."""
        with self._lock:
            if self.phase not in {"invalid", "finished"}:
                self.phase = "draining"

    @property
    def is_invalid(self) -> bool:
        with self._lock:
            return self.phase == "invalid"

    def finish(self) -> None:
        with self._lock:
            self.phase = "finished"


_RUNTIME: CampaignRuntime | None = None


class ResponseTally:
    """Keep only sanitized response dimensions needed by final benchmark artifacts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._response_codes: Counter[tuple[int, str]] = Counter()
        self._operational_results: Counter[str] = Counter()

    def record(self, status_code: int, body: str, *, webhook: bool = False) -> None:
        payload = _decoded_json(body)
        problem_code = ""
        if isinstance(payload, dict) and isinstance(payload.get("code"), str):
            problem_code = cast(str, payload["code"])
        with self._lock:
            self._response_codes[(status_code, problem_code)] += 1
            if webhook:
                self._operational_results[_operational_outcome(status_code, payload)] += 1

    def write(self, response_path: Path, operational_path: Path) -> None:
        response_path.parent.mkdir(parents=True, exist_ok=True)
        with response_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(("status_code", "problem_code", "count"))
            for (status_code, problem_code), count in sorted(self._response_codes.items()):
                writer.writerow((status_code, problem_code, count))
        required = (
            "APPLIED",
            "NO_STATE_CHANGE",
            "IGNORED_STALE",
            "IGNORED_INVALID_TRANSITION",
            "DUPLICATE",
            "REJECTED",
        )
        outcomes = set(required) | set(self._operational_results)
        with operational_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(("result", "count"))
            for outcome in sorted(outcomes):
                writer.writerow((outcome, self._operational_results[outcome]))


_TALLY = ResponseTally()


class FulfillFlowBenchmarkUser(HttpUser):
    """Exercise only public HTTP routes; it never imports internals or accesses SQL."""

    wait_time = between(0.001, 0.005)

    def on_start(self) -> None:
        runtime = _require_runtime()
        self.assignment = runtime.register_user()
        self.measurement_request_index = 0
        self.measurement_event_sequence = 0
        self.mutation_halted = False

    @task
    def execute_contract_request(self) -> None:
        runtime = _require_runtime()
        if runtime.phase in {"barrier", "draining", "finished", "invalid"}:
            gevent.sleep(0.01)
            return
        if runtime.phase == "warmup":
            self._warmup_step(runtime)
            return
        if not runtime.begin_request():
            return
        plan = request_plan(runtime.bundle.manifest.profile)
        offset = (self.assignment.index * 37) % len(plan)
        kind = plan[(self.measurement_request_index + offset) % len(plan)]
        self.measurement_request_index += 1
        try:
            if kind == "shipment_detail":
                self._shipment_detail(runtime)
            elif kind == "timeline":
                self._timeline(runtime)
            elif kind == "shipment_list":
                self._shipment_list(runtime)
            else:
                self.measurement_event_sequence += 1
                self._webhook(
                    runtime,
                    slot=self.assignment.measurement,
                    phase="measure",
                    sequence=self.measurement_event_sequence,
                )
        finally:
            runtime.finish_request()

    def _warmup_step(self, runtime: CampaignRuntime) -> None:
        completed = runtime.warmup_completed_for(self.assignment.index)
        quota = runtime.bundle.manifest.warmup_quota_per_shipment
        if completed >= quota:
            gevent.sleep(0.01)
            return
        assert runtime.phase_started_at is not None
        # The final interval is admission headroom, not a response deadline:
        # requests admitted before 60 seconds may finish during the bounded drain.
        scheduled = runtime.phase_started_at + _warmup_scheduled_offset(
            completed,
            quota,
            runtime.load.users,
            self.assignment.index,
        )
        remaining = scheduled - time.monotonic()
        if remaining > 0:
            gevent.sleep(min(remaining, 0.05))
            return
        if not runtime.begin_request():
            return
        try:
            if self._webhook(
                runtime,
                slot=self.assignment.warmup,
                phase="warmup",
                sequence=completed + 1,
            ):
                runtime.record_warmup_applied(self.assignment.index)
        finally:
            runtime.finish_request()

    def _shipment_detail(self, runtime: CampaignRuntime) -> None:
        shipment_id = self.assignment.timeline_shipment_id
        with self.client.get(
            f"/api/v1/shipments/{shipment_id}",
            name="GET /api/v1/shipments/{shipment_id}",
            catch_response=True,
            timeout=runtime.bundle.manifest.timeouts.request_seconds,
        ) as response:
            _TALLY.record(response.status_code, response.text)
            if response.status_code != 200:
                response.failure(f"expected 200, received {response.status_code}")

    def _timeline(self, runtime: CampaignRuntime) -> None:
        shipment_id = self.assignment.timeline_shipment_id
        with self.client.get(
            f"/api/v1/shipments/{shipment_id}/tracking?page=1&page_size=25",
            name="GET /api/v1/shipments/{shipment_id}/tracking",
            catch_response=True,
            timeout=runtime.bundle.manifest.timeouts.request_seconds,
        ) as response:
            _TALLY.record(response.status_code, response.text)
            if response.status_code != 200:
                response.failure(f"expected 200, received {response.status_code}")

    def _shipment_list(self, runtime: CampaignRuntime) -> None:
        with self.client.get(
            "/api/v1/shipments?status=PENDING&page=1&page_size=25",
            name="GET /api/v1/shipments?status={status}",
            catch_response=True,
            timeout=runtime.bundle.manifest.timeouts.request_seconds,
        ) as response:
            _TALLY.record(response.status_code, response.text)
            if response.status_code != 200:
                response.failure(f"expected 200, received {response.status_code}")

    def _webhook(
        self,
        runtime: CampaignRuntime,
        *,
        slot: CohortSlot,
        phase: Literal["warmup", "measure"],
        sequence: int,
    ) -> bool:
        if self.mutation_halted:
            return False
        try:
            status = _target_status(slot.initial_status, sequence)
            event_id = deterministic_event_id(
                runtime.bundle.manifest.profile,
                runtime.load.name,
                phase,
                slot.slot_id,
                sequence,
            )
            base = (
                runtime.bundle.manifest.warmup_occurred_at_base
                if phase == "warmup"
                else runtime.bundle.manifest.measurement_occurred_at_base
            )
            occurred_at = base + timedelta(
                microseconds=(sequence * runtime.bundle.manifest.occurred_at_step_microseconds)
            )
            payload = _payload(slot, event_id, status, occurred_at)
            raw_body = canonical_payload_bytes(payload)
            timestamp = str(int(time.time()))
            secret = _carrier_secret(slot.carrier_code)
            signed = timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + raw_body
            signature = (
                "sha256=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
            )
            headers = {
                "Content-Type": "application/json",
                "X-FulfillFlow-Event-Id": event_id,
                "X-FulfillFlow-Timestamp": timestamp,
                "X-FulfillFlow-Signature": signature,
            }
            with self.client.post(
                f"/api/v1/carriers/{slot.carrier_code}/events",
                data=raw_body,
                headers=headers,
                name="POST /api/v1/carriers/{carrier_code}/events",
                catch_response=True,
                timeout=runtime.bundle.manifest.timeouts.request_seconds,
            ) as response:
                _TALLY.record(response.status_code, response.text, webhook=True)
                failure = _webhook_failure(response.status_code, response.text)
                if failure is not None:
                    response.failure(failure)
                    self.mutation_halted = True
                    runtime.invalidate(
                        f"slot {slot.slot_id} stopped after a non-APPLIED webhook response"
                    )
                    return False
            return True
        except Exception as exc:
            self.mutation_halted = True
            runtime.invalidate(f"slot {slot.slot_id} stopped after {type(exc).__name__}")
            raise


def _target_status(initial_status: str, sequence: int) -> str:
    return cycle_target_status(initial_status, sequence)


def _warmup_scheduled_offset(
    sequence_index: int,
    quota: int,
    user_count: int,
    slot_rank: int,
) -> float:
    """Assign each campaign event a unique deterministic instant inside 60 seconds."""
    if quota <= 0 or user_count <= 0 or not 0 <= slot_rank < user_count:
        raise ValueError("warm-up schedule inputs are invalid")
    global_index = (sequence_index * user_count) + slot_rank
    return (global_index + 1) * 60 / ((user_count * quota) + 1)


def _balanced_active_slots(cohort: tuple[CohortSlot, ...], users: int) -> tuple[CohortSlot, ...]:
    """Compatibility wrapper retained for focused workload contract tests."""
    return balanced_active_slots(cohort, users)


def _event_id(
    profile: str,
    load_name: str,
    phase: str,
    slot_id: str,
    sequence: int,
) -> str:
    """Compatibility wrapper retained for focused workload contract tests."""
    return deterministic_event_id(profile, load_name, phase, slot_id, sequence)


def _payload(
    slot: CohortSlot,
    event_id: str,
    canonical_status: str,
    occurred_at: datetime,
) -> dict[str, object]:
    return carrier_payload(slot, event_id, canonical_status, occurred_at)


def _webhook_failure(status_code: int, body: str) -> str | None:
    if status_code != 200:
        return f"expected 200/APPLIED, received HTTP {status_code}"
    payload = _decoded_json(body)
    if payload is _INVALID_JSON:
        return "expected 200/APPLIED, received a non-JSON body"
    if not isinstance(payload, dict):
        return f"expected 200/APPLIED, received {_json_shape(payload)}"
    result = payload.get("result")
    if result != "APPLIED":
        label = result if isinstance(result, str) else _json_shape(result)
        return f"expected 200/APPLIED, received result={label}"
    return None


class _InvalidJson:
    pass


_INVALID_JSON = _InvalidJson()


def _decoded_json(body: str) -> object:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return _INVALID_JSON


def _json_shape(value: object) -> str:
    if value is _INVALID_JSON:
        return "invalid JSON"
    if value is None:
        return "JSON null"
    if isinstance(value, bool):
        return "JSON boolean"
    if isinstance(value, list):
        return "JSON array"
    if isinstance(value, str):
        return "JSON string"
    if isinstance(value, (int, float)):
        return "JSON number"
    if isinstance(value, dict):
        return "JSON object"
    return "unexpected JSON value"


def _operational_outcome(status_code: int, payload: object) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        return cast(str, payload["result"])
    if status_code == 422 and isinstance(payload, dict):
        return "REJECTED"
    if status_code != 200:
        return f"HTTP_{status_code}"
    return _json_shape(payload).upper().replace(" ", "_")


def _carrier_secret(carrier_code: str) -> str:
    variable = (
        "CARRIER_ALPHA_WEBHOOK_SECRET"
        if carrier_code == "carrier-alpha"
        else "CARRIER_BETA_WEBHOOK_SECRET"
    )
    secret = os.environ.get(variable)
    if not secret:
        raise RuntimeError(f"{variable} is required by the load generator")
    return secret


def _require_runtime() -> CampaignRuntime:
    if _RUNTIME is None:
        raise RuntimeError("Locust campaign runtime was not initialized")
    return _RUNTIME


def _selected_load(bundle: CampaignBundle) -> LoadLevel:
    name = os.environ.get("BENCHMARK_LOAD_NAME")
    if not name:
        raise RuntimeError("BENCHMARK_LOAD_NAME is required")
    try:
        return next(item for item in bundle.manifest.loads if item.name == name)
    except StopIteration:
        raise RuntimeError(f"load level {name!r} is not present in the campaign") from None


def _initialize(environment: Environment, **_kwargs: object) -> None:
    global _RUNTIME
    manifest_path = os.environ.get("BENCHMARK_CAMPAIGN_MANIFEST")
    if not manifest_path:
        raise RuntimeError("BENCHMARK_CAMPAIGN_MANIFEST is required")
    bundle = load_campaign(Path(manifest_path))
    process_phase = os.environ.get("BENCHMARK_PHASE")
    if process_phase not in {"warmup", "measurement"}:
        raise RuntimeError("BENCHMARK_PHASE must be warmup or measurement")
    _RUNTIME = CampaignRuntime(bundle, _selected_load(bundle), cast(ProcessPhase, process_phase))
    _write_runtime_file("BENCHMARK_PID_FILE", str(os.getpid()))
    # Locust infers this configurable module value as int although gevent accepts floats.
    locust_stats.CSV_STATS_INTERVAL_SEC = float(bundle.manifest.collection_interval_seconds)  # type: ignore[assignment]
    if environment.parsed_options is None:
        raise RuntimeError("Locust parsed options are unavailable")
    environment.parsed_options.num_users = _RUNTIME.load.users
    environment.parsed_options.spawn_rate = _RUNTIME.load.spawn_rate


def _start_coordinator(environment: Environment, **_kwargs: object) -> None:
    gevent.spawn(_coordinate_campaign, environment, _require_runtime())


def _coordinate_campaign(environment: Environment, runtime: CampaignRuntime) -> None:
    while not runtime.all_users_ready and not runtime.is_invalid:
        gevent.sleep(0.01)
    if runtime.is_invalid:
        _quit_runner(environment, invalid=True)
        return
    runtime.begin_phase(runtime.process_phase)
    _write_runtime_file("BENCHMARK_PHASE_MARKER", datetime.now(UTC).isoformat())
    duration = (
        runtime.bundle.manifest.warmup_seconds
        if runtime.process_phase == "warmup"
        else runtime.bundle.manifest.measurement_seconds
    )
    deadline = cast(float, runtime.phase_started_at) + duration
    while time.monotonic() < deadline and not runtime.is_invalid:
        gevent.sleep(0.01 if runtime.process_phase == "warmup" else 0.05)
    if runtime.is_invalid:
        _quit_runner(environment, invalid=True)
        return
    runtime.stop_new_requests()
    drain_deadline = time.monotonic() + runtime.bundle.manifest.timeouts.drain_seconds
    while runtime.in_flight and time.monotonic() < drain_deadline and not runtime.is_invalid:
        gevent.sleep(0.01)
    if runtime.in_flight:
        runtime.invalidate("request drain timeout expired with requests still in flight")
        _quit_runner(environment, invalid=True)
        return
    if runtime.process_phase == "warmup" and not runtime.warmup_complete:
        runtime.invalidate("warm-up quota was incomplete at the fixed 60-second boundary")
        _quit_runner(environment, invalid=True)
        return
    runtime.finish()
    _write_runtime_file("BENCHMARK_PHASE_COMPLETED_MARKER", datetime.now(UTC).isoformat())
    _quit_runner(environment, invalid=runtime.is_invalid)


def _quit_runner(environment: Environment, *, invalid: bool = False) -> None:
    if invalid:
        environment.process_exit_code = 2
    if environment.runner is not None:
        environment.runner.quit()


def _write_runtime_file(variable: str, content: str) -> None:
    raw_path = os.environ.get(variable)
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8", newline="\n")


def _write_response_artifacts(environment: Environment, **_kwargs: object) -> None:
    del environment
    response = os.environ.get("BENCHMARK_RESPONSE_CODES_FILE")
    operational = os.environ.get("BENCHMARK_OPERATIONAL_RESULTS_FILE")
    if response and operational:
        _TALLY.write(Path(response), Path(operational))


events.init.add_listener(_initialize)  # type: ignore[no-untyped-call]
events.test_start.add_listener(_start_coordinator)  # type: ignore[no-untyped-call]
events.test_stop.add_listener(_write_response_artifacts)  # type: ignore[no-untyped-call]

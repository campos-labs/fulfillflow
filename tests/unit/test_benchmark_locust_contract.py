"""Locust workload weights, cycle, HMAC payloads, and isolation boundary."""

from __future__ import annotations

import ast
import json
import os
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# Importing Locust normally patches the entire process for load execution. Contract tests
# explicitly disable that process-wide side effect before importing the locustfile.
os.environ["LOCUST_SKIP_MONKEY_PATCH"] = "1"

from benchmarks.campaign import CampaignBundle, CampaignManifest, CohortSlot, load_campaign
from benchmarks.locustfile import (
    MIXED_REQUEST_PLAN,
    CampaignRuntime,
    FulfillFlowBenchmarkUser,
    ResponseTally,
    _coordinate_campaign,
    _event_id,
    _initialize,
    _payload,
    _quit_runner,
    _target_status,
    _warmup_scheduled_offset,
    _webhook_failure,
    effective_request_weights,
)
from locust import stats as locust_stats

FIXTURE = Path("benchmarks/fixtures/smoke-campaign.json")


@lru_cache(maxsize=1)
def _bundle() -> CampaignBundle:
    return load_campaign(FIXTURE)


def test_mixed_plan_has_exact_per_request_weights() -> None:
    assert len(MIXED_REQUEST_PLAN) == 100
    assert effective_request_weights(MIXED_REQUEST_PLAN) == {
        "shipment_detail": 25,
        "shipment_list": 20,
        "timeline": 25,
        "webhook": 30,
    }


@pytest.mark.parametrize(
    ("profile", "weights"),
    [
        (
            "timeline",
            {"shipment_detail": 50, "timeline": 50, "webhook": 0, "shipment_list": 0},
        ),
        (
            "ingestion",
            {"shipment_detail": 0, "timeline": 0, "webhook": 100, "shipment_list": 0},
        ),
    ],
)
def test_profile_manifest_weights_must_match_the_executable_plan(
    profile: str,
    weights: dict[str, int],
) -> None:
    fixture = json.loads(Path("benchmarks/fixtures/smoke-campaign.json").read_text())
    fixture["profile"] = profile
    fixture["weights"] = weights
    manifest = CampaignManifest.model_validate(fixture)
    bundle = _bundle()
    adjusted = bundle.__class__(
        manifest,
        bundle.warmup,
        bundle.measurement,
        bundle.timeline,
        bundle.dataset_path,
        bundle.dataset_sha256,
        bundle.dataset_document,
    )

    CampaignRuntime(adjusted, manifest.loads[0], "measurement")

    wrong = manifest.model_copy(
        update={"weights": manifest.weights.model_copy(update={"webhook": 99})}
    )
    with pytest.raises(ValueError, match="executable request plan"):
        CampaignRuntime(
            adjusted.__class__(
                wrong,
                adjusted.warmup,
                adjusted.measurement,
                adjusted.timeline,
                adjusted.dataset_path,
                adjusted.dataset_sha256,
                adjusted.dataset_document,
            ),
            wrong.loads[0],
            "measurement",
        )


def test_mutation_cycle_alternates_and_even_quota_restores_initial_state() -> None:
    for initial in ("IN_TRANSIT", "OUT_FOR_DELIVERY"):
        sequence = [_target_status(initial, number) for number in range(1, 7)]
        assert sequence[-1] == initial
        assert all(
            current != previous
            for previous, current in zip([initial, *sequence[:-1]], sequence, strict=True)
        )
        assert set(sequence) == {"IN_TRANSIT", "OUT_FOR_DELIVERY"}


def test_multiuser_warmup_offsets_are_globally_unique_and_distributed() -> None:
    for users, quota in ((4, 2), (188, 10)):
        per_user = {
            rank: [
                _warmup_scheduled_offset(sequence, quota, users, rank) for sequence in range(quota)
            ]
            for rank in range(users)
        }
        flattened = [offset for offsets in per_user.values() for offset in offsets]
        assert len(set(flattened)) == users * quota
        assert all(offsets == sorted(offsets) for offsets in per_user.values())
        assert min(flattened) > 0
        assert max(flattened) < 60
        assert sorted(flattened) == pytest.approx(
            [(index + 1) * 60 / ((users * quota) + 1) for index in range(users * quota)]
        )


def test_active_users_receive_exclusive_balanced_slots_in_both_phases() -> None:
    bundle = _bundle()
    runtime = CampaignRuntime(bundle, bundle.manifest.loads[0], "measurement")
    assignments = [runtime.register_user() for _ in range(4)]

    assert len({item.warmup.shipment_id for item in assignments}) == 4
    assert len({item.measurement.shipment_id for item in assignments}) == 4
    assert Counter(
        (item.warmup.carrier_code, item.warmup.initial_status) for item in assignments
    ) == {
        ("carrier-alpha", "IN_TRANSIT"): 1,
        ("carrier-alpha", "OUT_FOR_DELIVERY"): 1,
        ("carrier-beta", "IN_TRANSIT"): 1,
        ("carrier-beta", "OUT_FOR_DELIVERY"): 1,
    }
    assert Counter(
        (item.measurement.carrier_code, item.measurement.initial_status) for item in assignments
    ) == {
        ("carrier-alpha", "IN_TRANSIT"): 1,
        ("carrier-alpha", "OUT_FOR_DELIVERY"): 1,
        ("carrier-beta", "IN_TRANSIT"): 1,
        ("carrier-beta", "OUT_FOR_DELIVERY"): 1,
    }


def test_event_ids_are_deterministic_phase_separated_and_release_free() -> None:
    warmup = _event_id("mixed", "synthetic-load", "warmup", "warmup-alpha-it-000001", 1)
    measure = _event_id("mixed", "synthetic-load", "measure", "measure-alpha-it-000001", 1)

    assert warmup != measure
    assert warmup == _event_id("mixed", "synthetic-load", "warmup", "warmup-alpha-it-000001", 1)
    assert "release" not in warmup
    assert "repetition" not in warmup
    assert warmup.isascii() and len(warmup) <= 128


def test_both_carrier_payloads_encode_only_the_approved_cycle() -> None:
    occurred_at = datetime(2026, 9, 1, tzinfo=UTC)
    alpha = CohortSlot(
        slot_id="alpha",
        shipment_id="00000000-0000-4000-8000-000000000001",
        carrier_code="carrier-alpha",
        tracking_code="ALPHA-BENCH-000001",
        initial_status="IN_TRANSIT",
        initial_occurred_at="2026-08-30T00:00:00Z",
    )
    beta = CohortSlot(
        slot_id="beta",
        shipment_id="00000000-0000-4000-8000-000000000002",
        carrier_code="carrier-beta",
        tracking_code="BETA-BENCH-000002",
        initial_status="OUT_FOR_DELIVERY",
        initial_occurred_at="2026-08-30T00:00:00Z",
    )

    assert _payload(alpha, "event-a", "IN_TRANSIT", occurred_at)["status"] == "MOVING"
    beta_payload = _payload(beta, "event-b", "OUT_FOR_DELIVERY", occurred_at)
    assert beta_payload["event"] == {
        "type": "courier_route",
        "occurred_at": "2026-09-01T00:00:00.000000Z",
        "details": "Deterministic benchmark event",
    }


def test_only_200_applied_is_success() -> None:
    assert _webhook_failure(200, json.dumps({"result": "APPLIED"})) is None
    assert _webhook_failure(200, json.dumps({"result": "DUPLICATE"})) is not None
    assert _webhook_failure(200, json.dumps({"result": "NO_STATE_CHANGE"})) is not None
    assert _webhook_failure(500, "{}") is not None


@pytest.mark.parametrize(
    ("body", "diagnostic"),
    [
        ("[]", "JSON array"),
        ('"text"', "JSON string"),
        ("1", "JSON number"),
        ("true", "JSON boolean"),
        ("null", "JSON null"),
        ("not-json", "non-JSON"),
        ("{}", "result=JSON null"),
    ],
)
def test_unexpected_webhook_json_shapes_fail_with_stable_diagnostics(
    body: str,
    diagnostic: str,
) -> None:
    assert diagnostic in (_webhook_failure(200, body) or "")


def test_measurement_admission_closes_before_in_flight_requests_are_drained() -> None:
    bundle = _bundle()
    runtime = CampaignRuntime(bundle, bundle.manifest.loads[0], "measurement")
    runtime.begin_phase("measurement")

    assert runtime.begin_request() is True  # one read
    assert runtime.begin_request() is True  # one webhook
    runtime.stop_new_requests()
    assert runtime.begin_request() is False
    assert runtime.in_flight == 2
    runtime.finish_request()
    runtime.finish_request()
    assert runtime.in_flight == 0


def test_measurement_coordinator_drains_accepted_request_before_quit_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    runtime = CampaignRuntime(bundle, bundle.manifest.loads[0], "measurement")
    for _ in range(runtime.load.users):
        runtime.register_user()
    clock = {"value": 0.0}
    order: list[str] = []
    tally = ResponseTally()

    def monotonic() -> float:
        return clock["value"]

    def sleep(_duration: float) -> None:
        if runtime.phase == "measurement" and runtime.in_flight == 0:
            assert runtime.begin_request() is True
            order.append("request-started")
        if runtime.phase == "measurement":
            clock["value"] += 150.0
        elif runtime.phase == "draining" and runtime.in_flight:
            assert runtime.begin_request() is False
            tally.record(200, '{"result":"APPLIED"}', webhook=True)
            runtime.finish_request()
            order.append("request-finished")
            clock["value"] += 0.01

    class Runner:
        def quit(self) -> None:
            order.append("runner-quit")

    environment = SimpleNamespace(process_exit_code=None, runner=Runner())
    monkeypatch.setattr("benchmarks.locustfile.time.monotonic", monotonic)
    monkeypatch.setattr("benchmarks.locustfile.gevent.sleep", sleep)

    _coordinate_campaign(environment, runtime)  # type: ignore[arg-type]
    tally.write(tmp_path / "responses.csv", tmp_path / "operational.csv")

    assert order == ["request-started", "request-finished", "runner-quit"]
    assert runtime.in_flight == 0
    assert "APPLIED,1" in (tmp_path / "operational.csv").read_text(encoding="utf-8")


def test_measurement_coordinator_invalidates_when_drain_timeout_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    runtime = CampaignRuntime(bundle, bundle.manifest.loads[0], "measurement")
    for _ in range(runtime.load.users):
        runtime.register_user()
    clock = {"value": 0.0}

    def sleep(_duration: float) -> None:
        if runtime.phase == "measurement" and runtime.in_flight == 0:
            assert runtime.begin_request() is True
        clock["value"] += 150.0 if runtime.phase == "measurement" else 11.0

    runner = Mock()
    environment = SimpleNamespace(process_exit_code=None, runner=runner)
    monkeypatch.setattr("benchmarks.locustfile.time.monotonic", lambda: clock["value"])
    monkeypatch.setattr("benchmarks.locustfile.gevent.sleep", sleep)

    _coordinate_campaign(environment, runtime)  # type: ignore[arg-type]

    assert runtime.is_invalid is True
    assert environment.process_exit_code == 2
    runner.quit.assert_called_once_with()


def test_warmup_quota_can_complete_during_drain_without_new_admissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    runtime = CampaignRuntime(bundle, bundle.manifest.loads[0], "warmup")
    for index in range(runtime.load.users):
        runtime.register_user()
        for _ in range(bundle.manifest.warmup_quota_per_shipment - (index == 0)):
            runtime.record_warmup_applied(index)
    clock = {"value": 0.0}

    def sleep(_duration: float) -> None:
        if runtime.phase == "warmup":
            assert runtime.begin_request()
            clock["value"] = 60.0
        elif runtime.phase == "draining":
            assert not runtime.begin_request()
            assert not runtime.warmup_complete
            clock["value"] += 1.0  # Exceeds 159 ms; admitted work still has drain budget.
            runtime.record_warmup_applied(0)
            runtime.finish_request()

    environment = SimpleNamespace(process_exit_code=None, runner=Mock())
    monkeypatch.setattr("benchmarks.locustfile.time.monotonic", lambda: clock["value"])
    monkeypatch.setattr("benchmarks.locustfile.gevent.sleep", sleep)
    _coordinate_campaign(environment, runtime)  # type: ignore[arg-type]
    assert runtime.warmup_complete
    assert not runtime.is_invalid
    assert runtime.in_flight == 0
    environment.runner.quit.assert_called_once_with()


@pytest.mark.parametrize(
    "kind",
    ["shipment_detail", "timeline", "shipment_list", "webhook"],
)
def test_every_request_kind_uses_the_same_admission_and_in_flight_counter(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    runtime = CampaignRuntime(bundle, bundle.manifest.loads[0], "measurement")
    assignment = runtime.register_user()
    runtime.begin_phase("measurement")
    observed: list[str] = []

    def record(name: str) -> None:
        assert runtime.in_flight == 1
        observed.append(name)

    user = SimpleNamespace(
        assignment=assignment,
        measurement_request_index=0,
        measurement_event_sequence=0,
        mutation_halted=False,
        _shipment_detail=lambda _runtime: record("shipment_detail"),
        _timeline=lambda _runtime: record("timeline"),
        _shipment_list=lambda _runtime: record("shipment_list"),
        _webhook=lambda _runtime, **_kwargs: record("webhook"),
    )
    monkeypatch.setattr("benchmarks.locustfile._RUNTIME", runtime)
    monkeypatch.setattr("benchmarks.locustfile.request_plan", lambda _profile: (kind,))

    FulfillFlowBenchmarkUser.execute_contract_request(user)  # type: ignore[arg-type]

    assert observed == [kind]
    assert runtime.in_flight == 0


def test_invalid_campaign_sets_a_nonzero_process_exit_code() -> None:
    runner = Mock()
    environment = SimpleNamespace(process_exit_code=None, runner=runner)

    _quit_runner(environment, invalid=True)  # type: ignore[arg-type]

    assert environment.process_exit_code == 2
    runner.quit.assert_called_once_with()


def test_manifest_collection_interval_is_applied_to_locust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed_options = SimpleNamespace(num_users=0, spawn_rate=0.0)
    environment = SimpleNamespace(parsed_options=parsed_options)
    monkeypatch.setenv("BENCHMARK_CAMPAIGN_MANIFEST", "benchmarks/fixtures/smoke-campaign.json")
    monkeypatch.setenv("BENCHMARK_LOAD_NAME", "synthetic-4-users")
    monkeypatch.setenv("BENCHMARK_PHASE", "measurement")

    _initialize(environment)  # type: ignore[arg-type]

    assert locust_stats.CSV_STATS_INTERVAL_SEC == 1.0
    assert parsed_options.num_users == 4
    assert parsed_options.spawn_rate == 2.0


def test_locustfile_has_no_application_internal_or_database_import() -> None:
    imports: set[str] = set()
    for path in (
        Path("benchmarks/locustfile.py"),
        Path("benchmarks/campaign.py"),
        Path("benchmarks/artifact.py"),
        Path("benchmarks/database_contract.py"),
        Path("benchmarks/semantic.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert not any(name == "fulfillflow" or name.startswith("fulfillflow.") for name in imports)
    assert not any(
        name == forbidden or name.startswith(f"{forbidden}.")
        for forbidden in ("sqlalchemy", "psycopg")
        for name in imports
    )

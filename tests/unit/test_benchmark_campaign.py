"""Campaign schema and cross-manifest cohort validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from benchmarks.artifact import BENCHMARK_HASH_NAME, canonical_json_bytes
from benchmarks.campaign import CampaignManifest, load_campaign
from pydantic import ValidationError

FIXTURE = Path("benchmarks/fixtures/smoke-campaign.json")


def test_synthetic_campaign_validates_complete_protocol_and_cohorts() -> None:
    bundle = load_campaign(FIXTURE)

    assert bundle.manifest.official is False
    assert bundle.manifest.workers == 1
    assert bundle.manifest.warmup_seconds == 60
    assert bundle.manifest.measurement_seconds == 300
    assert bundle.dataset_sha256 == bundle.manifest.cohorts.dataset_sha256
    assert bundle.manifest.warmup_quota_per_shipment % 2 == 0
    assert len(bundle.warmup) == 188
    assert len(bundle.measurement) == 188
    assert {item.shipment_id for item in bundle.warmup}.isdisjoint(
        item.shipment_id for item in bundle.measurement
    )
    mutable = {item.shipment_id for item in bundle.warmup + bundle.measurement}
    assert mutable.isdisjoint(item.shipment_id for item in bundle.timeline)


def test_official_campaign_requires_exactly_five_repetitions() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["official"] = True
    payload["repetitions"] = 4

    with pytest.raises(ValidationError, match="exactly five"):
        CampaignManifest.model_validate(payload)


def test_warmup_quota_must_be_even() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["warmup_quota_per_shipment"] = 3

    with pytest.raises(ValidationError, match="must be even"):
        CampaignManifest.model_validate(payload)


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan")])
def test_stabilization_must_be_finite_and_nonnegative(seconds: float) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["stabilization_seconds"] = seconds
    with pytest.raises(ValidationError, match="stabilization_seconds"):
        CampaignManifest.model_validate(payload)


def test_official_requires_positive_stabilization_and_essential_host_declarations() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(official=True, repetitions=5)
    with pytest.raises(ValidationError, match="stabilization_seconds must be positive"):
        CampaignManifest.model_validate(payload)
    payload["stabilization_seconds"] = 1
    with pytest.raises(ValidationError, match="essential host expectation"):
        CampaignManifest.model_validate(payload)


@pytest.mark.parametrize(
    "field,duration", [("warmup_process_seconds", 60), ("measurement_process_seconds", 300)]
)
def test_process_timeout_strictly_contains_phase_and_drain(field: str, duration: int) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    boundary = duration + payload["timeouts"]["drain_seconds"]
    for value in (boundary - 1, boundary):
        payload["timeouts"][field] = value
        with pytest.raises(ValidationError, match="must exceed"):
            CampaignManifest.model_validate(payload)
    payload["timeouts"][field] = boundary + 0.001
    assert CampaignManifest.model_validate(payload).timeouts.model_dump()[field] == boundary + 0.001


def test_campaign_requires_a_well_formed_release_specific_schema_digest() -> None:
    missing = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del missing["database"]["schema_sha256"]
    malformed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    malformed["database"]["schema_sha256"] = "not-a-sha256"

    with pytest.raises(ValidationError, match="schema_sha256"):
        CampaignManifest.model_validate(missing)
    with pytest.raises(ValidationError, match="schema_sha256"):
        CampaignManifest.model_validate(malformed)


def test_campaign_requires_supported_structural_contract_and_alembic_head() -> None:
    version = json.loads(FIXTURE.read_text(encoding="utf-8"))
    version["database"]["structural_contract_version"] = 2
    no_heads = json.loads(FIXTURE.read_text(encoding="utf-8"))
    no_heads["database"]["alembic_heads"] = []

    with pytest.raises(ValidationError, match="structural contract version"):
        CampaignManifest.model_validate(version)
    with pytest.raises(ValidationError, match="alembic_heads"):
        CampaignManifest.model_validate(no_heads)


def test_users_cannot_exceed_exclusive_cohort_capacity() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["loads"][0]["users"] = 189

    with pytest.raises(ValidationError, match="cannot exceed 188"):
        CampaignManifest.model_validate(payload)


def test_users_must_preserve_four_way_mutable_balance() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["loads"][0]["users"] = 2

    with pytest.raises(ValidationError, match="multiple of four"):
        CampaignManifest.model_validate(payload)


def test_request_weights_are_executable_contract_not_free_configuration() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["weights"]["webhook"] = 29
    payload["weights"]["shipment_list"] = 21

    with pytest.raises(ValidationError, match="request weights"):
        CampaignManifest.model_validate(payload)


def test_campaign_rejects_noncanonical_or_truncated_dataset_bytes(tmp_path: Path) -> None:
    campaign = _copy_authenticated_campaign(tmp_path)
    dataset = tmp_path / "benchmark-v1.0.json"
    dataset.write_bytes(dataset.read_bytes() + b"\n")

    with pytest.raises(ValueError, match=r"canonical|sidecar"):
        load_campaign(campaign)


def test_campaign_rejects_relation_mutation_even_with_updated_sidecar(tmp_path: Path) -> None:
    campaign = _copy_authenticated_campaign(tmp_path)
    dataset = tmp_path / "benchmark-v1.0.json"
    document = json.loads(dataset.read_text(encoding="utf-8"))
    non_applied = next(
        item
        for item in document["tables"]["tracking_events"]
        if item["application_result"] != "APPLIED"
    )
    document["tables"]["notifications"][0]["tracking_event_id"] = non_applied["id"]
    document["tables"]["notifications"][0]["shipment_id"] = non_applied["shipment_id"]
    _rewrite_authenticated_artifact(campaign, dataset, document)

    with pytest.raises(ValueError, match=r"Notification|APPLIED"):
        load_campaign(campaign)


def test_campaign_rejects_semantic_result_swap_with_all_digests_updated(
    tmp_path: Path,
) -> None:
    campaign = _copy_authenticated_campaign(tmp_path)
    dataset = tmp_path / "benchmark-v1.0.json"
    document = json.loads(dataset.read_text(encoding="utf-8"))
    events = document["tables"]["tracking_events"]
    no_state = next(item for item in events if item["application_result"] == "NO_STATE_CHANGE")
    stale = next(item for item in events if item["application_result"] == "IGNORED_STALE")
    no_state["application_result"] = "IGNORED_STALE"
    stale["application_result"] = "NO_STATE_CHANGE"
    _rewrite_authenticated_artifact(campaign, dataset, document)

    with pytest.raises(ValueError, match=r"replay|NO_STATE_CHANGE|IGNORED_STALE"):
        load_campaign(campaign)


def test_campaign_rejects_colliding_slot_identity_with_valid_digest(tmp_path: Path) -> None:
    campaign = _copy_authenticated_campaign(tmp_path)
    dataset = tmp_path / "benchmark-v1.0.json"
    document = json.loads(dataset.read_text(encoding="utf-8"))
    warmup = document["cohorts"]["mutable_component"]["warmup"]
    warmup[1]["slot_id"] = warmup[0]["slot_id"]
    _rewrite_authenticated_artifact(campaign, dataset, document)

    with pytest.raises(ValueError, match=r"slot|cohort"):
        load_campaign(campaign)


def test_campaign_rejects_overlapping_shipment_with_valid_digest(tmp_path: Path) -> None:
    campaign = _copy_authenticated_campaign(tmp_path)
    dataset = tmp_path / "benchmark-v1.0.json"
    document = json.loads(dataset.read_text(encoding="utf-8"))
    mutable = document["cohorts"]["mutable_component"]
    warmup_slot = mutable["warmup"][0]
    measurement_slot = mutable["measurement"][0]
    for field in (
        "shipment_id",
        "carrier_code",
        "tracking_code",
        "initial_status",
        "initial_occurred_at",
    ):
        measurement_slot[field] = warmup_slot[field]
    _rewrite_authenticated_artifact(campaign, dataset, document)

    with pytest.raises(ValueError, match=r"overlap|duplicate|cohort"):
        load_campaign(campaign)


def _copy_authenticated_campaign(tmp_path: Path) -> Path:
    source_dataset = Path("benchmarks/datasets/benchmark-v1.0.json")
    source_sidecar = Path("benchmarks/datasets") / BENCHMARK_HASH_NAME
    dataset = tmp_path / source_dataset.name
    sidecar = tmp_path / source_sidecar.name
    dataset.write_bytes(source_dataset.read_bytes())
    sidecar.write_bytes(source_sidecar.read_bytes())
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["cohorts"]["dataset_manifest"] = dataset.name
    campaign = tmp_path / "campaign.json"
    campaign.write_text(json.dumps(payload), encoding="utf-8")
    return campaign


def _rewrite_authenticated_artifact(
    campaign: Path,
    dataset: Path,
    document: dict[str, object],
) -> None:
    canonical = canonical_json_bytes(document)
    digest = hashlib.sha256(canonical).hexdigest()
    dataset.write_bytes(canonical)
    dataset.with_name(BENCHMARK_HASH_NAME).write_text(digest + "\n", encoding="ascii")
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    payload["cohorts"]["dataset_sha256"] = digest
    campaign.write_text(json.dumps(payload), encoding="utf-8")

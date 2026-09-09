"""Explicit owner identities, unchanged workload, and complete aggregate telemetry."""

import csv
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from benchmarks import prepare_v11
from benchmarks.campaign import CampaignManifest, SplitDatabases, SplitResources
from benchmarks.collectors import ExternalCommandError
from benchmarks.collectors_v11 import aggregate_resources
from benchmarks.prepare_v11 import compose_prefix
from pydantic import ValidationError


def candidate_payload() -> dict:
    payload = json.loads(Path("benchmarks/campaigns/v1-baseline-mixed.json").read_text())
    payload.update(
        schema_version=2,
        release="v1.1.0",
        internal_timeouts={"core_seconds": 6, "tracking_seconds": 8},
    )
    for key in ("resources", "images"):
        value = payload[key].pop("app")
        payload[key].update(core=value, tracking=deepcopy(value))
    payload["resources"].update(
        core={"cpus": "1.0", "memory": "768m"}, tracking={"cpus": "1.0", "memory": "768m"}
    )
    pool = dict(payload["pool"], size=5)
    payload["pool"] = {"core": pool, "tracking": deepcopy(pool)}
    database = payload["database"]
    payload["database"] = {
        "core": dict(database, alembic_heads=["1101_core"]),
        "tracking": dict(database, alembic_heads=["1101_tracking"]),
    }
    payload["environment"]["services"] = {
        "app": None,
        "core": "core",
        "tracking": "tracking",
        "postgres": "db",
        "loadgen": "loadgen",
    }
    return payload


def test_v11_requires_own_explicit_topology_and_preserves_protocol() -> None:
    payload = candidate_payload()
    manifest = CampaignManifest.model_validate(payload)
    assert isinstance(manifest.database, SplitDatabases)
    assert isinstance(manifest.resources, SplitResources)
    baseline = json.loads(Path("benchmarks/campaigns/v1-baseline-mixed.json").read_text())
    for key in ("loads", "weights", "host", "timeouts", "telemetry", "cohorts"):
        assert payload[key] == baseline[key]
    for key, value in (("release", "v1.0.0"), ("schema_version", 1), ("internal_timeouts", None)):
        with pytest.raises(ValidationError):
            CampaignManifest.model_validate(dict(payload, **{key: value}))


@pytest.mark.parametrize("core,tracking", [(10, 30), (6, 10), (8, 8), (float("nan"), 8)])
def test_internal_deadlines_cannot_exceed_frozen_public_budget(
    core: float, tracking: float
) -> None:
    payload = candidate_payload()
    payload["internal_timeouts"] = {"core_seconds": core, "tracking_seconds": tracking}
    with pytest.raises(ValidationError):
        CampaignManifest.model_validate(payload)


@pytest.mark.parametrize(
    "project", ["fulfillflow", "fulfillflow-v11", "fulfillflow-benchmark", "../v11"]
)
def test_preparation_cannot_target_existing_historical_or_local_projects(project: str) -> None:
    with pytest.raises(ValueError):
        compose_prefix(project)


def test_restore_refuses_unowned_existing_project_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "preexisting\n", "")

    monkeypatch.setattr(prepare_v11, "ROOT", tmp_path)
    monkeypatch.setattr(prepare_v11, "run_capture", run)
    for owner in ("core", "tracking"):
        monkeypatch.setenv(
            f"{owner.upper()}_DATABASE_URL",
            f"postgresql+psycopg://fulfillflow_{owner}:synthetic@db:5432/fulfillflow_{owner}",
        )
        monkeypatch.setenv(f"{owner.upper()}_DB_PASSWORD", "synthetic")
    with pytest.raises(ValueError, match="preexisting"):
        prepare_v11.restore("fulfillflow-ii-test", "fulfillflow-ii-test")
    assert not any("down" in argv for argv in calls)
    assert not list(tmp_path.rglob("*.json"))


def test_owner_budget_cannot_multiply_the_application_allocation() -> None:
    payload = candidate_payload()
    payload["pool"]["tracking"]["size"] = 10
    with pytest.raises(ValidationError, match="aggregate"):
        CampaignManifest.model_validate(payload)


def test_aggregate_requires_complete_samples_and_sums_both_owners(tmp_path: Path) -> None:
    source, destination = tmp_path / "resources.csv", tmp_path / "aggregate.csv"
    fields = [
        "timestamp_utc",
        "service",
        "container_id",
        "cpu_percent",
        "memory_usage_bytes",
        "memory_limit_bytes",
        "postgres_active_connections",
    ]
    ids = {role: f"{role}-id" for role in ("core", "tracking", "postgres", "loadgen")}
    rows = [
        [
            "2026-09-09T12:00:00+00:00",
            role,
            identifier,
            "25",
            "100",
            "200",
            "2" if role != "loadgen" else "",
        ]
        for role, identifier in ids.items()
    ]
    with source.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)
    aggregate_resources(source, destination, ids)
    with destination.open() as stream:
        aggregate = next(csv.DictReader(stream))
    assert aggregate["service"] == "application_total"
    assert float(aggregate["cpu_percent"]) == 50
    assert float(aggregate["memory_usage_bytes"]) == 200
    assert float(aggregate["postgres_active_connections"]) == 4
    with source.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows[:-1])
    with pytest.raises(ExternalCommandError):
        aggregate_resources(source, destination, ids)

"""Offline consolidation only. Reads frozen artifacts; writes exclusively beside this script."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics as st
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import NotRequired, TypedDict, cast


class FileRecord(TypedDict):
    bytes: int
    sha256: str


type Inventory = dict[str, FileRecord]


class ResourceMetrics(TypedDict):
    cycles: int
    span_seconds: float
    gap_min_s: float
    gap_median_s: float
    gap_max_s: float
    cpu_weighted_mean_percent: float
    cpu_core_seconds_observed_span: float
    cpu_max_percent: float
    memory_mean_mib: float
    memory_peak_mib: float
    memory_limit_bytes: list[int]
    postgres_active_connections_max: int | None


class SessionPlan(TypedDict):
    results: str
    manifest: str
    manifest_sha256: str
    campaign: str
    repetitions: list[str]


class Packet(TypedDict):
    sessions: dict[str, SessionPlan]
    uv_lock_sha256: str
    evidence_sha256: dict[str, str]
    prior_integrity_sha256: NotRequired[str]


class Manifest(TypedDict):
    git_sha: str
    official: bool
    repetitions: int
    profile: str
    warmup_quota_per_shipment: int


class PriorSource(TypedDict):
    path: str
    inventory: Inventory


class PriorIntegrity(TypedDict):
    sources: list[PriorSource]


class Receipt(TypedDict):
    verified: bool
    results: Inventory
    original_results: Inventory
    internal_copy: Inventory
    internal_source_before: Inventory
    internal_source_after: Inventory


class Observation(TypedDict):
    matches: bool | None
    observed: object


class GitIdentity(TypedDict):
    sha: str
    staged_clean: bool
    worktree_clean: bool


class Phase(TypedDict):
    exit_code: int


class StructuralSchema(TypedDict):
    matches: bool
    alembic_matches: bool
    observed: str


class InitialIdentity(TypedDict):
    structural_schema: StructuralSchema


class DatabaseSnapshot(TypedDict):
    label: str
    metrics: dict[str, int]


class Metadata(TypedDict):
    valid: bool
    official: bool
    manifest_sha256: str
    git: GitIdentity
    campaign: str
    profile: str
    warmup: Phase
    measurement: Phase
    load: dict[str, str]
    repetition: int
    environment_checks: dict[str, Observation]
    host_identity: dict[str, Observation]
    host_state: dict[str, Observation]
    protocol_expected: dict[str, object]
    dataset_sha256: str
    logical_database_identity: dict[str, InitialIdentity]
    stabilization: dict[str, int | float | str]
    database_snapshots: list[DatabaseSnapshot]
    container_ids: dict[str, str]


class Run(TypedDict):
    profile: str
    users: int
    repetition: int
    requests: int
    throughput_locust_rps: float
    completed_requests_per_300s: float
    p50_ms: float
    p95_ms: float
    error_rate: float
    warmup_applied: int
    measurement_applied: int
    stabilization: dict[str, int | float | str]
    warmup: Phase
    measurement: Phase
    resources: dict[str, dict[str, ResourceMetrics]]
    host_state_observed: dict[str, object]
    host_identity: dict[str, Observation]
    manifest_sha256: str


ARCHIVE = Path()
ROOT = Path()
OUT = Path(__file__).resolve().parent
HEAD = "ae15e0a2da465f4aec3d9c699655441ad1947265"
DATASET = "5897d7441f73fec77d98ff97196aff0becc3f301e45c708febff493d8f4a63bf"
SCHEMA = "0ee99170b78404380541ac12cf676126d047fc60275a4e2947fbe5a68b32652b"


def read_json(path: Path) -> object:
    # The casts at call sites describe frozen artifact shapes, not new acceptance rules.
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def dispersion(values: Sequence[float]) -> dict[str, float]:
    assert len(values) == 5 and all(math.isfinite(v) for v in values)
    q1, _, q3 = st.quantiles(values, n=4, method="inclusive")
    mean = st.mean(values)
    return {
        "n": 5,
        "median": st.median(values),
        "mean": mean,
        "sample_sd": st.stdev(values),
        "cv_percent": 100 * st.stdev(values) / mean if mean else 0,
        "min": min(values),
        "max": max(values),
        "q1_inclusive": q1,
        "q3_inclusive": q3,
        "iqr_inclusive": q3 - q1,
    }


def validate_resource_samples(path: Path, container_ids: Mapping[str, str]) -> None:
    """Require nonempty, complete sampling cycles; do not invent cadence tolerances."""
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream)
            timestamp: datetime | None = None
            services: set[str] = set()
            for row in rows:
                instant = datetime.fromisoformat(row["timestamp_utc"])
                if instant.utcoffset() is None:
                    raise ValueError
                if instant != timestamp:
                    if timestamp is not None and (
                        instant <= timestamp or services != set(container_ids)
                    ):
                        raise ValueError
                    timestamp, services = instant, set()
                service = row["service"]
                if service in services or row["container_id"] != container_ids[service]:
                    raise ValueError
                cpu = float(row["cpu_percent"])
                if not math.isfinite(cpu) or cpu < 0:
                    raise ValueError
                if int(row["memory_usage_bytes"]) < 0 or int(row["memory_limit_bytes"]) <= 0:
                    raise ValueError
                connections = row["postgres_active_connections"]
                if service == "postgres":
                    if int(connections) < 0:
                        raise ValueError
                elif connections != "":
                    raise ValueError
                services.add(service)
            if timestamp is None or services != set(container_ids):
                raise ValueError
    except (OSError, KeyError, TypeError, ValueError, csv.Error) as exc:
        raise ValueError("resource CSV is missing or has incomplete/invalid samples") from exc


def resource_metrics(path: Path, ids: Mapping[str, str]) -> dict[str, ResourceMetrics]:
    validate_resource_samples(path, ids)
    result: dict[str, ResourceMetrics] = {}
    for service in ("app", "postgres", "loadgen"):
        data = [x for x in rows(path) if x["service"] == service]
        timestamps = [datetime.fromisoformat(x["timestamp_utc"]).timestamp() for x in data]
        delta = [b - a for a, b in pairwise(timestamps)]
        assert len(delta) > 0 and min(delta) > 0
        cpu = [float(x["cpu_percent"]) for x in data]
        # Each CPU sample describes a preceding counter interval: use right endpoints.
        core_seconds = sum(c * dt / 100 for c, dt in zip(cpu[1:], delta, strict=True))
        result[service] = {
            "cycles": len(data),
            "span_seconds": sum(delta),
            "gap_min_s": min(delta),
            "gap_median_s": st.median(delta),
            "gap_max_s": max(delta),
            "cpu_weighted_mean_percent": core_seconds / sum(delta) * 100,
            "cpu_core_seconds_observed_span": core_seconds,
            "cpu_max_percent": max(cpu),
            "memory_mean_mib": st.mean(int(x["memory_usage_bytes"]) for x in data) / 2**20,
            "memory_peak_mib": max(int(x["memory_usage_bytes"]) for x in data) / 2**20,
            "memory_limit_bytes": sorted({int(x["memory_limit_bytes"]) for x in data}),
            "postgres_active_connections_max": max(
                int(x["postgres_active_connections"]) for x in data
            )
            if service == "postgres"
            else None,
        }
    return result


def inventory(root: Path) -> Inventory:
    require(root.is_dir() and root.resolve().is_relative_to(ARCHIVE))
    result: Inventory = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink() and not path.is_junction())
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": sha(path),
            }
    return result


def require(value: object) -> None:
    if not value:
        raise ValueError("Archived evidence failed verification")


def legacy_location(value: str) -> Path:
    # Historical receipts are immutable. Resolve their known evidence suffixes, never their drive.
    parts = value.replace("\\", "/").split("/")
    if "benchmarks" in parts:
        return ARCHIVE / "repository" / "/".join(parts[parts.index("benchmarks") :])
    for i, part in enumerate(parts):
        if part.startswith("fulfillflow-official-v1.0"):
            prefix = (
                Path("diagnostics/recovery01")
                if part.endswith("-recovery01")
                else Path("operations") / part
            )
            return ARCHIVE / prefix / "/".join(parts[i + 1 :])
    raise ValueError("Unknown historical evidence location")


class ArchiveSession:
    def __init__(self, folder: str) -> None:
        self.PACKAGE = ARCHIVE / "operations" / folder

    def packet(self) -> Packet:
        return cast(Packet, read_json(self.PACKAGE / "packet.json"))

    def plan(self, profile: str) -> SessionPlan:
        return self.packet()["sessions"][profile]

    def root_for(self, profile: str) -> Path:
        path = (ROOT / self.plan(profile)["results"]).resolve()
        require(path.is_relative_to(ROOT / "benchmarks/results"))
        return path

    def archive_for(self, profile: str) -> Path:
        suffix = "complement01" if "complement01" in self.PACKAGE.name else "attempt01"
        return self.PACKAGE / "artifacts" / f"{profile}-{suffix}"

    def validate(self, profile: str) -> None:
        p = self.plan(profile)
        manifest = ROOT / p["manifest"]
        require(sha(manifest) == p["manifest_sha256"])
        # This Git export uses CRLF; the measured worktree lockfile used LF.
        require(sha(ARCHIVE / "worktree-inputs/uv.lock") == self.packet()["uv_lock_sha256"])
        data = cast(Manifest, read_json(manifest))
        require(data["git_sha"] == HEAD and data["official"] is True and data["repetitions"] == 5)
        require(data["profile"] == profile and data["warmup_quota_per_shipment"] == 430)
        for name, digest in self.packet()["evidence_sha256"].items():
            require(sha(self.PACKAGE / "evidence" / name) == digest)

    def verify_predecessors(self) -> None:
        packet = self.packet()
        if "prior_integrity_sha256" not in packet:
            return
        path = self.PACKAGE / "prior-integrity.json"
        require(sha(path) == packet["prior_integrity_sha256"])
        for item in cast(PriorIntegrity, read_json(path))["sources"]:
            require(inventory(legacy_location(item["path"])) == item["inventory"])

    def verify_repetition(self, profile: str, name: str) -> Metadata:
        p = self.plan(profile)
        require(name in p["repetitions"])
        root = self.root_for(profile) / name
        checks = (root / "checksums.sha256").read_text().splitlines()
        require(len(checks) == 16)
        names = set()
        for line in checks:
            digest, relative = line.split("  ", 1)
            file = (root / relative).resolve()
            require(file.is_relative_to(root) and relative not in names)
            require(sha(file) == digest)
            names.add(relative)
        require(set(inventory(root)) == names | {"checksums.sha256"})
        meta = cast(Metadata, read_json(root / "metadata.json"))
        require(meta["valid"] is True and meta["official"] is True)
        require(meta["manifest_sha256"] == p["manifest_sha256"] and meta["git"]["sha"] == HEAD)
        require(meta["campaign"] == p["campaign"] and meta["profile"] == profile)
        require(meta["warmup"]["exit_code"] == meta["measurement"]["exit_code"] == 0)
        require(name == f"{meta['load']['name']}-r{meta['repetition']:02d}")
        require(all(x["matches"] is True for x in meta["environment_checks"].values()))
        require(all(x["matches"] is True for x in meta["host_identity"].values()))
        expected = cast(dict[str, object], read_json(ROOT / p["manifest"]))
        expected.pop("loads")
        require(meta["protocol_expected"] == expected)
        return meta

    def verify_session_copy(self, profile: str) -> None:
        dest = self.archive_for(profile)
        receipt = cast(Receipt, read_json(dest / "session-integrity.json"))
        require(
            receipt["verified"]
            and receipt["results"]
            == inventory(self.root_for(profile))
            == inventory(dest / "session-results-original")
        )
        require(not any(self.root_for(profile).rglob(".incomplete.json")))
        for name in self.plan(profile)["repetitions"]:
            self.verify_repetition(profile, name)
            copy = dest / name
            proof = cast(Receipt, read_json(copy / "integrity.json"))
            require(
                proof["verified"]
                and proof["original_results"] == inventory(copy / "results-original")
            )
            require(
                proof["internal_copy"]
                == proof["internal_source_before"]
                == proof["internal_source_after"]
                == inventory(copy / "loadgen-internal")
            )

    inventory = staticmethod(inventory)


def helper(folder: str, name: str) -> ArchiveSession:
    return ArchiveSession(folder)


def portable[T](value: T) -> T:
    if isinstance(value, dict):
        return cast(T, {portable(k): portable(v) for k, v in value.items()})
    if isinstance(value, list):
        return cast(T, [portable(x) for x in value])
    if isinstance(value, str):
        prefix = str(ARCHIVE)
        if value == prefix or value.startswith(prefix + "/") or value.startswith(prefix + "\\"):
            return cast(T, Path(value).relative_to(ARCHIVE).as_posix())
    return value


def consolidate() -> tuple[dict[str, object], dict[str, object]]:
    sessions = [
        ("fulfillflow-official-v1.0", "mixed", (4,)),
        ("fulfillflow-official-v1.0-mixed12-complement01", "mixed", (12,)),
        ("fulfillflow-official-v1.0-timeline01", "timeline", (4, 12)),
        ("fulfillflow-official-v1.0-ingestion01", "ingestion", (4, 12)),
    ]
    runs: list[Run] = []
    index: list[dict[str, object]] = []
    proofs: list[dict[str, str]] = []
    tracked_sources = {}
    common_initial = None
    for number, (folder, profile, users_set) in enumerate(sessions):
        mod = helper(folder, f"ops{number}")
        mod.validate(profile)
        if number:
            mod.verify_session_copy(profile)
        if hasattr(mod, "verify_predecessors"):
            mod.verify_predecessors()
        for source_root in (mod.root_for(profile), mod.archive_for(profile)):
            for source in source_root.iterdir():
                if source.is_file():
                    tracked_sources[str(source)] = sha(source)
        for marker in mod.root_for(profile).glob("*.partial/.incomplete.json"):
            tracked_sources[str(marker)] = sha(marker)
        manifest = ROOT / mod.plan(profile)["manifest"]
        tracked_sources[str(manifest)] = sha(manifest)
        for line in (mod.PACKAGE / "SHA256SUMS").read_text().splitlines():
            h, relative = line.split("  ", 1)
            source = mod.PACKAGE / relative
            assert sha(source) == h
            tracked_sources[str(source)] = h
        proofs.append(
            {
                "package": str(mod.PACKAGE),
                "seal_sha256": sha(mod.PACKAGE / "SHA256SUMS"),
                "manifest": str(manifest),
                "manifest_sha256": sha(manifest),
            }
        )
        for users in users_set:
            for repetition in range(1, 6):
                name = f"{profile}-{users}-users-r{repetition:02d}"
                root = mod.root_for(profile) / name
                meta = mod.verify_repetition(profile, name)
                inv = mod.inventory(root)
                for relative, item in inv.items():
                    tracked_sources[str(root / relative)] = item["sha256"]
                copied = mod.archive_for(profile) / name
                receipt = cast(Receipt, read_json(copied / "integrity.json"))
                assert receipt["verified"] and receipt["original_results"] == inv == mod.inventory(
                    copied / "results-original"
                )
                assert (
                    receipt["internal_copy"]
                    == receipt["internal_source_before"]
                    == receipt["internal_source_after"]
                    == mod.inventory(copied / "loadgen-internal")
                )
                tracked_sources[str(copied / "integrity.json")] = sha(copied / "integrity.json")
                for folder_name in ("results-original", "loadgen-internal"):
                    for relative, item in mod.inventory(copied / folder_name).items():
                        tracked_sources[str(copied / folder_name / relative)] = item["sha256"]
                assert (
                    meta["git"]["sha"] == HEAD
                    and meta["git"]["staged_clean"]
                    and meta["git"]["worktree_clean"]
                )
                assert meta["dataset_sha256"] == DATASET
                schema = meta["logical_database_identity"]["initial"]["structural_schema"]
                assert (
                    schema["matches"] and schema["alembic_matches"] and schema["observed"] == SCHEMA
                )
                logical_initial = meta["logical_database_identity"]["initial"]
                if common_initial is None:
                    common_initial = logical_initial
                assert logical_initial == common_initial
                assert meta["stabilization"]["expected_seconds"] == 300
                assert (
                    meta["protocol_expected"]["images"]
                    == cast(dict[str, object], read_json(manifest))["images"]
                )
                assert meta["protocol_expected"]["workers"] == 1
                for field in ("ac_power", "power_plan_guid", "concurrent_containers"):
                    assert meta["host_state"][field]["matches"] is True
                warm_expected = users * 430
                warm = rows(root / "warmup/operational_results.http.csv")
                assert (
                    next(int(x["count"]) for x in warm if x["result"] == "APPLIED") == warm_expected
                )
                assert all(int(x["count"]) == 0 for x in warm if x["result"] != "APPLIED")
                stats = rows(root / "locust_stats.csv")
                aggregate = next(x for x in stats if x["Name"] == "Aggregated")
                count = int(aggregate["Request Count"])
                assert int(aggregate["Failure Count"]) == 0
                codes = rows(root / "response_codes.csv")
                assert all(x["status_code"] == "200" for x in codes)
                assert sum(int(x["count"]) for x in codes) == count
                warm_codes = rows(root / "warmup/response_codes.csv")
                assert all(x["status_code"] == "200" for x in warm_codes)
                assert sum(int(x["count"]) for x in warm_codes) == warm_expected
                op = rows(root / "operational_results.csv")
                posts = sum(int(x["Request Count"]) for x in stats if x["Type"] == "POST")
                applied_http = sum(
                    int(x["count"])
                    for x in op
                    if x["source"] == "http" and x["result"] == "APPLIED"
                )
                applied_db = sum(
                    int(x["count"])
                    for x in op
                    if x["source"] == "database_tracking" and x["result"] == "APPLIED"
                )
                assert posts == applied_http == applied_db
                assert all(int(x["count"]) == 0 for x in op if x["result"] != "APPLIED")
                snapshots = {x["label"]: x["metrics"] for x in meta["database_snapshots"]}
                for key in (
                    "table.tracking_events",
                    "table.carrier_event_inbox",
                    "table.notifications",
                ):
                    assert (
                        snapshots["pre_measurement"][key] - snapshots["initial"][key]
                        == warm_expected
                    )
                    assert (
                        snapshots["post_measurement"][key] - snapshots["pre_measurement"][key]
                        == posts
                    )
                if profile == "timeline":
                    assert snapshots["pre_measurement"] == snapshots["post_measurement"]
                finals = list((copied / "loadgen-internal").rglob("locust_final_stats.csv"))
                assert len(finals) == 2
                assert any(sha(f) == sha(root / "locust_stats.csv") for f in finals)
                assert any(sha(f) == sha(root / "warmup/locust_stats.csv") for f in finals)
                resources = {
                    phase: resource_metrics(root / relative, meta["container_ids"])
                    for phase, relative in [
                        ("warmup", "warmup/resources.csv"),
                        ("measurement", "resources.csv"),
                    ]
                }
                state = {k: v["observed"] for k, v in meta["host_state"].items()}
                run: Run = {
                    "profile": profile,
                    "users": users,
                    "repetition": repetition,
                    "requests": count,
                    "throughput_locust_rps": float(aggregate["Requests/s"]),
                    "completed_requests_per_300s": count / 300,
                    "p50_ms": float(aggregate["Median Response Time"]),
                    "p95_ms": float(aggregate["95%"]),
                    "error_rate": 0.0,
                    "warmup_applied": warm_expected,
                    "measurement_applied": posts,
                    "stabilization": meta["stabilization"],
                    "warmup": meta["warmup"],
                    "measurement": meta["measurement"],
                    "resources": resources,
                    "host_state_observed": state,
                    "host_identity": meta["host_identity"],
                    "manifest_sha256": meta["manifest_sha256"],
                }
                runs.append(run)
                index.append(
                    {
                        "profile": profile,
                        "users": users,
                        "repetition": repetition,
                        "result": str(root),
                        "manifest": str(manifest),
                        "manifest_sha256": sha(manifest),
                        "metadata_sha256": sha(root / "metadata.json"),
                        "checksums_sha256": sha(root / "checksums.sha256"),
                        "copy_receipt": str(copied / "integrity.json"),
                        "copy_receipt_sha256": sha(copied / "integrity.json"),
                        "original_files": inv,
                        "accepted": True,
                    }
                )
    assert len(runs) == len({(x["profile"], x["users"], x["repetition"]) for x in runs}) == 30
    summaries = []
    for profile in ("mixed", "timeline", "ingestion"):
        for users in (4, 12):
            group = [x for x in runs if x["profile"] == profile and x["users"] == users]
            metrics = {
                k: dispersion([x[k] for x in group])
                for k in (
                    "throughput_locust_rps",
                    "completed_requests_per_300s",
                    "p50_ms",
                    "p95_ms",
                )
            }
            summary_resources = {
                service: {
                    "cpu_weighted_mean_percent_median": st.median(
                        x["resources"]["measurement"][service]["cpu_weighted_mean_percent"]
                        for x in group
                    ),
                    "memory_peak_mib_all_repetitions": max(
                        x["resources"]["measurement"][service]["memory_peak_mib"] for x in group
                    ),
                    "sampling_gap_max_s": max(
                        x["resources"]["measurement"][service]["gap_max_s"] for x in group
                    ),
                    "cycles_each_repetition": [
                        x["resources"]["measurement"][service]["cycles"] for x in group
                    ],
                }
                for service in ("app", "postgres", "loadgen")
            }
            summaries.append(
                {
                    "profile": profile,
                    "users": users,
                    "metrics": metrics,
                    "resources": summary_resources,
                    "errors": 0,
                    "requests_total": sum(x["requests"] for x in group),
                }
            )
    assert all(sha(Path(p)) == h for p, h in tracked_sources.items())
    return {
        "head": HEAD,
        "dataset_sha256": DATASET,
        "schema_sha256": SCHEMA,
        "valid_repetitions": 30,
        "groups": summaries,
        "runs": runs,
    }, {
        "accepted": index,
        "packages": proofs,
        "excluded": [
            {
                "path": str(
                    ROOT
                    / "benchmarks/results/v1-baseline-mixed-attempt01/mixed-12-users-r01.partial"
                ),
                "reason": "preparation refused; no measurement; original cause not confirmed",
            }
        ],
        "prior_diagnostics_counted": 0,
        "source_files_before_after_sha256": tracked_sources,
    }


def main() -> None:
    global ARCHIVE, ROOT, OUT
    if not __debug__:
        raise SystemExit("Do not disable evidence assertions with -O")
    parser = argparse.ArgumentParser(
        description="Offline baseline reconstruction: Python stdlib only"
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    ARCHIVE = args.archive.resolve()
    ROOT = ARCHIVE / "repository"
    OUT = args.output.resolve()
    require(ROOT.is_dir())

    assert dispersion([1, 2, 3, 4, 5])["iqr_inclusive"] == 2
    assert dispersion([4] * 5)["cv_percent"] == 0
    summary, index = consolidate()
    outputs = {"summary.json": portable(summary), "run-index.json": portable(index)}
    if args.verify_only:
        for name, content in outputs.items():
            assert read_json(OUT / name) == content
        print("RECOMPUTED_REPORT_MATCHES=True; SOURCES_UNCHANGED=True")
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        assert not any((OUT / name).exists() for name in outputs)
        for name, content in outputs.items():
            with (OUT / name).open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(content, stream, indent=2, sort_keys=True)
                stream.write("\n")
        print("OFFLINE_REPORT_CREATED=True; ACCEPTED=30; GROUPS=6; SOURCES_UNCHANGED=True")


if __name__ == "__main__":
    main()

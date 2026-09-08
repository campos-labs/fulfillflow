"""Standard-library tests of the offline publication, never of the workload."""

import csv
import importlib.util
import math
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from importlib.machinery import ModuleSpec, SourceFileLoader
from pathlib import Path
from typing import Protocol, TypedDict, cast


class Report(Protocol):
    def dispersion(self, values: Sequence[float]) -> dict[str, float]: ...

    def resource_metrics(
        self, path: Path, ids: Mapping[str, str]
    ) -> dict[str, dict[str, object]]: ...

    def legacy_location(self, value: str) -> Path: ...

    def read_json(self, path: Path) -> object: ...


class IndexEntry(TypedDict):
    profile: str
    users: int
    repetition: int
    result: str
    manifest: str
    copy_receipt: str


class PublicIndex(TypedDict):
    accepted: list[IndexEntry]
    prior_diagnostics_counted: int


spec = cast(
    ModuleSpec,
    importlib.util.spec_from_file_location("report", Path(__file__).with_name("consolidate.py")),
)
report_module = importlib.util.module_from_spec(spec)
cast(SourceFileLoader, spec.loader).exec_module(report_module)
report = cast(Report, report_module)


class OfflineTests(unittest.TestCase):
    def test_descriptive_statistics(self) -> None:
        result = report.dispersion([1, 2, 3, 4, 5])
        self.assertEqual((result["median"], result["iqr_inclusive"]), (3, 2))
        self.assertAlmostEqual(result["sample_sd"], math.sqrt(2.5))
        self.assertEqual(report.dispersion([5] * 5)["cv_percent"], 0)

    def test_five_observations_required(self) -> None:
        with self.assertRaises(AssertionError):
            report.dispersion([1, 2])

    def sample(
        self, directory: str, omit: str | None = None, negative: bool = False
    ) -> tuple[Path, dict[str, str]]:
        path = Path(directory) / "resources.csv"
        ids = {x: x + "-synthetic" for x in ("app", "postgres", "loadgen")}
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "timestamp_utc",
                    "service",
                    "container_id",
                    "cpu_percent",
                    "memory_usage_bytes",
                    "memory_limit_bytes",
                    "postgres_active_connections",
                ]
            )
            for i in range(2):
                for service, cid in ids.items():
                    if i == 1 and service == omit:
                        continue
                    cpu = -1 if negative else (200 if i == 0 else 100)
                    writer.writerow(
                        [
                            f"2026-09-08T00:00:0{i}+00:00",
                            service,
                            cid,
                            cpu,
                            1024,
                            2048,
                            1 if service == "postgres" else "",
                        ]
                    )
        return path, ids

    def test_cpu_right_endpoint_no_boundary_extrapolation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ff-offline-test-") as directory:
            path, ids = self.sample(directory)
            result = report.resource_metrics(path, ids)["app"]
            self.assertEqual(result["span_seconds"], 1)
            self.assertEqual(result["cpu_core_seconds_observed_span"], 1)
            self.assertEqual(result["cpu_weighted_mean_percent"], 100)

    def test_missing_component_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ff-offline-test-") as directory:
            path, ids = self.sample(directory, omit="loadgen")
            with self.assertRaises(ValueError):
                report.resource_metrics(path, ids)

    def test_negative_cpu_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ff-offline-test-") as directory:
            path, ids = self.sample(directory, negative=True)
            with self.assertRaises(ValueError):
                report.resource_metrics(path, ids)

    def test_unknown_legacy_location_refused(self) -> None:
        with self.assertRaises(ValueError):
            report.legacy_location("unrelated/input.json")

    def test_public_index_unique_portable_selection(self) -> None:
        index = cast(PublicIndex, report.read_json(Path(__file__).with_name("run-index.json")))
        self.assertEqual(len(index["accepted"]), 30)
        unique = {(x["profile"], x["users"], x["repetition"]) for x in index["accepted"]}
        self.assertEqual(len(unique), 30)
        for item in index["accepted"]:
            for key in ("result", "manifest", "copy_receipt"):
                self.assertNotIn(":", item[key])
                self.assertNotIn("\\", item[key])
                self.assertFalse(item[key].startswith("/"))
        self.assertEqual(index["prior_diagnostics_counted"], 0)


if __name__ == "__main__":
    unittest.main()

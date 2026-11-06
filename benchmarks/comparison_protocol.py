"""Explicit full comparison protocol; historical and sensitivity loaders stay separate."""

import json
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from benchmarks.campaign import CampaignBundle, CampaignContract, load_validated_bundle

PROTOCOL = "symmetric-warmup120-comparison-v1"


class ComparisonManifest(CampaignContract):
    protocol: Literal["symmetric-warmup120-comparison-v1"]
    warmup_seconds: Literal[120]

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if (
            not self.official
            or self.repetitions != 5
            or len(self.loads) != 1
            or self.loads[0].users not in (4, 12)
            or self.loads[0].spawn_rate != 16
            or self.warmup_quota_per_shipment != 430
            or self.stabilization_seconds != 300
            or self.collection_interval_seconds != 1
            or self.timeouts.warmup_process_seconds != 150
            or self.timeouts.measurement_process_seconds != 330
            or self.timeouts.request_seconds != 10
            or self.timeouts.drain_seconds != 10
        ):
            raise ValueError(
                "comparison requires frozen five-repetition 4/12 q430 120-second policy"
            )
        return self


def load_comparison_campaign(path: Path) -> CampaignBundle:
    manifest = ComparisonManifest.model_validate_json(path.read_text(encoding="utf-8"))
    return load_validated_bundle(path, manifest)


def verify_warmup_progress(directory: Path, users: int) -> None:
    """Additional drained per-user evidence; database identity gates remain mandatory."""
    progress = json.loads((directory / "warmup-progress.json").read_text(encoding="utf-8"))
    expected = {
        "schema_version": 1,
        "admission_seconds": 120,
        "registered_users": users,
        "in_flight": 0,
        "failure_code": None,
        "warmup_complete": True,
        "applied_by_user": [430] * users,
    }
    if progress != expected or any(type(n) is not int for n in progress["applied_by_user"]):
        raise ValueError("comparison warm-up requires exact complete per-user drained quota")

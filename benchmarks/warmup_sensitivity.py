"""Explicit exploratory protocol; historical CampaignManifest still accepts only 60 s."""

from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from benchmarks.campaign import CampaignBundle, CampaignContract, load_validated_bundle

PROTOCOL = "warmup-policy-sensitivity-v1"
ORDER: tuple[tuple[Literal["v10", "v11"], Literal[60, 120], int], ...] = (
    ("v10", 60, 1),
    ("v11", 120, 1),
    ("v11", 60, 1),
    ("v10", 120, 1),
    ("v10", 120, 2),
    ("v11", 60, 2),
    ("v11", 120, 2),
    ("v10", 60, 2),
)


class SensitivityManifest(CampaignContract):
    """Reuse identity/data/resource gates without widening the historical entry point."""

    protocol: Literal["warmup-policy-sensitivity-v1"]
    warmup_seconds: Literal[60, 120]

    @model_validator(mode="after")
    def validate_sensitivity(self) -> Self:
        if (
            self.official
            or self.repetitions != 1
            or self.profile != "mixed"
            or len(self.loads) != 1
            or self.loads[0].users != 12
            or self.loads[0].spawn_rate != 16
            or self.warmup_quota_per_shipment != 430
            or self.stabilization_seconds != 300
            or self.timeouts.warmup_process_seconds
            <= self.warmup_seconds + self.timeouts.drain_seconds
        ):
            raise ValueError("sensitivity requires nonofficial single mixed/12 q430 warm-up")
        return self


def load_sensitivity_campaign(path: Path) -> CampaignBundle:
    """Opt in explicitly; never auto-detect a protocol from unvalidated input."""
    manifest = SensitivityManifest.model_validate_json(path.read_text(encoding="utf-8"))
    return load_validated_bundle(path, manifest)

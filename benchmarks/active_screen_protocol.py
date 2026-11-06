"""Nonofficial active-screen diagnostic; historical loaders remain unchanged."""

from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from benchmarks.campaign import CampaignBundle, CampaignContract, load_validated_bundle
from benchmarks.comparison_protocol import ComparisonManifest

PROTOCOL = "active-screen-mixed4-diagnostic-v1"


class ActiveScreenManifest(CampaignContract):
    protocol: Literal["active-screen-mixed4-diagnostic-v1"]
    official: Literal[False]
    matrix_eligible: Literal[False]
    warmup_seconds: Literal[120]

    @model_validator(mode="after")
    def validate_diagnostic(self) -> Self:
        if self.release != "v1.1.0" or self.profile != "mixed" or self.loads[0].users != 4:
            raise ValueError("active-screen diagnostic requires v1.1 mixed/4")
        # Reuse all frozen numerical checks; this projection never becomes an artifact.
        frozen = self.model_dump(mode="json", exclude={"matrix_eligible"})
        frozen.update(official=True, protocol="symmetric-warmup120-comparison-v1")
        ComparisonManifest.model_validate(frozen)
        return self


def load_active_screen_campaign(path: Path) -> CampaignBundle:
    manifest = ActiveScreenManifest.model_validate_json(path.read_text(encoding="utf-8"))
    return load_validated_bundle(path, manifest)

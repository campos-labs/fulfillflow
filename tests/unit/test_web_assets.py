"""Integrity and package-resource coverage for the local web assets."""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files

import pytest


@pytest.mark.parametrize(
    ("name", "expected_sri"),
    [
        (
            "bootstrap-5.3.8.min.css",
            "sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB",
        ),
        (
            "bootstrap-5.3.8.bundle.min.js",
            "sha384-FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI",
        ),
        (
            "htmx-2.0.10.min.js",
            "sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V",
        ),
    ],
)
def test_local_vendor_asset_matches_official_distribution(
    name: str,
    expected_sri: str,
) -> None:
    """Keep the exact upstream bytes identified by their published SRI hashes."""
    asset = files("fulfillflow.web").joinpath("static", "vendor", name)
    digest = base64.b64encode(hashlib.sha384(asset.read_bytes()).digest()).decode("ascii")

    assert f"sha384-{digest}" == expected_sri


@pytest.mark.parametrize(
    ("name", "marker"),
    [
        ("BOOTSTRAP-LICENSE.txt", "The MIT License"),
        ("HTMX-LICENSE.txt", "Zero-Clause BSD"),
    ],
)
def test_vendor_license_is_packaged(name: str, marker: str) -> None:
    """Ship each upstream license beside the corresponding local asset."""
    license_file = files("fulfillflow.web").joinpath("static", "vendor", name)

    assert marker in license_file.read_text(encoding="utf-8")

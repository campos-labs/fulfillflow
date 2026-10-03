"""Distribution verification is offline; it does not repeat evaluated scenarios."""

import json
import shutil
from pathlib import Path

import pytest
from validation.functional.evidence.c.build_bundle import project
from validation.functional.evidence.c.reproduce import verify

BASE = Path(__file__).resolve().parents[1] / "evidence/c"


def test_distributed_matrix_is_reconstructed():
    result = verify(BASE)
    assert result["cases"] == 54
    assert sum(x["count"] for x in result["matrix"] if x["classification"] == "PASS") == 45
    assert sum(x["count"] for x in result["matrix"] if x["classification"] == "INCONCLUSIVE") == 9


def test_modified_archive_is_rejected(tmp_path):
    shutil.copyfile(BASE / "packages.json", tmp_path / "packages.json")
    (tmp_path / "evaluated.zip").write_bytes(b"changed")
    with pytest.raises(ValueError, match="archive hash"):
        verify(tmp_path)


def test_projection_preserves_json_and_original_bytes():
    original = json.dumps({"path": r"C:\Users\natoc\example", "pid": 123}).encode()
    distributed = project(original)
    assert json.loads(distributed) == {"path": r"<USER_HOME>\example", "pid": 123}
    assert b"natoc" in original and b"natoc" not in distributed
    assert project(distributed) == distributed

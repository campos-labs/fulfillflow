"""Real Git rejection tests, without application imports or external dependencies."""

import subprocess
from pathlib import Path

import pytest
from validation.functional.c_sources import REQUIRED, inventory, write_new


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "frozen source with spaces"
    repo.mkdir()
    git(repo, "init", "-q")
    for name in REQUIRED:
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("original\n", encoding="utf-8")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v1.0.0")
    return repo, sha


def test_inventory_uses_frozen_blobs_not_dirty_checkout(source: tuple[Path, str]) -> None:
    repo, sha = source
    before = inventory(repo, {"v1.0.0": sha})
    (repo / "uv.lock").write_text("unrelated dirty checkout", encoding="utf-8")
    after = inventory(repo, {"v1.0.0": sha})
    assert before == after
    assert after["evaluated_execution_authorized"] is False
    assert after["status"] == "SOURCE_INVENTORY_ONLY"


def test_rejects_wrong_sha(source: tuple[Path, str]) -> None:
    with pytest.raises(ValueError, match="FROZEN_TAG_MISMATCH"):
        inventory(source[0], {"v1.0.0": "0" * 40})


def test_rejects_missing_tag(source: tuple[Path, str]) -> None:
    with pytest.raises(ValueError, match="FROZEN_GIT_READ_FAILED"):
        inventory(source[0], {"v9.0.0": source[1]})


def test_rejects_missing_contract(source: tuple[Path, str]) -> None:
    repo, _ = source
    git(repo, "rm", "DESIGN.md")
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "missing contract",
    )
    git(repo, "tag", "v2.0.0")
    with pytest.raises(ValueError, match="REQUIRED_SOURCE_MISSING"):
        inventory(repo, {"v2.0.0": git(repo, "rev-parse", "HEAD")})


def test_preserves_existing_output(tmp_path: Path) -> None:
    p = tmp_path / "inventory.json"
    write_new(p, {"original": True})
    before = p.read_bytes()
    with pytest.raises(FileExistsError):
        write_new(p, {"original": False})
    assert p.read_bytes() == before


@pytest.mark.parametrize(
    "refs,code",
    [
        ({}, "EMPTY_REFERENCES"),
        ({"--help": "0" * 40}, "INVALID_REFERENCE"),
        ({"v1.0.0": "HEAD"}, "INVALID_SHA"),
    ],
)
def test_rejects_unbounded_reference(tmp_path: Path, refs: dict[str, str], code: str) -> None:
    with pytest.raises(ValueError, match=code):
        inventory(tmp_path, refs)


def test_export_preserves_bytes_and_detects_modification(source, tmp_path, monkeypatch):
    from validation.functional import c_runtime

    repo, sha = source
    monkeypatch.setitem(c_runtime.REFERENCES, "v1.0.0", sha)
    destination = tmp_path / "export with spaces"
    record = c_runtime.export_source(repo, "v1.0.0", destination)
    c_runtime.verify_export(record)
    assert record["sha"] == sha
    (destination / "uv.lock").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="FROZEN_EXPORT_CHANGED"):
        c_runtime.verify_export(record)


def test_export_refuses_reuse(source, tmp_path, monkeypatch):
    from validation.functional import c_runtime

    repo, sha = source
    monkeypatch.setitem(c_runtime.REFERENCES, "v1.0.0", sha)
    destination = tmp_path / "preserved"
    destination.mkdir()
    sentinel = destination / "keep"
    sentinel.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        c_runtime.export_source(repo, "v1.0.0", destination)
    assert sentinel.read_bytes() == b"original"


def test_export_rejects_added_python_module(source, tmp_path, monkeypatch):
    from validation.functional import c_runtime

    repo, sha = source
    monkeypatch.setitem(c_runtime.REFERENCES, "v1.0.0", sha)
    record = c_runtime.export_source(repo, "v1.0.0", tmp_path / "export")
    (Path(record["source"]) / "src/fulfillflow/untracked.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="FROZEN_EXPORT_CHANGED"):
        c_runtime.verify_export(record)

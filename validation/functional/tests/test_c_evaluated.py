"""Evaluated ordering and frozen package refusal, without application execution."""

import hashlib
import json

import pytest
from validation.functional import c_evaluated


def test_fixed_54_sequence():
    cases = c_evaluated.sequence()
    assert len(cases) == 54 and len({x["destination"] for x in cases}) == 54
    assert [x["action"] for x in cases[::3]] == [
        "healthy",
        "duplicate",
        "conflict",
        "control",
        "kill",
        "unavailable",
    ] * 3
    assert all(sum(x["version"] == v for x in cases) == 18 for v in c_evaluated.REFERENCES)


def test_unreleased_package_cannot_execute(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    with pytest.raises(FileNotFoundError):
        c_evaluated.execute(tmp_path)


def test_changed_tool_package_blocks_before_output(tmp_path, monkeypatch):
    destination = tmp_path / "never"
    manifest = {
        "tools": {},
        "references": c_evaluated.REFERENCES,
        "cases": c_evaluated.sequence(),
        "destination": str(destination),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    (tmp_path / "release.json").write_text(
        json.dumps(
            {"approved": True, "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    )
    monkeypatch.setattr(c_evaluated, "tool_hashes", lambda: {"changed": "sha"})
    with pytest.raises(ValueError, match="PACKAGE_IDENTITY"):
        c_evaluated.execute(tmp_path)
    assert not destination.exists()


def released_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(c_evaluated, "BASE", tmp_path)
    monkeypatch.setattr(c_evaluated, "tool_hashes", lambda: {"tool": "fixed"})
    (tmp_path / "README.md").write_text("frozen protocol")
    (tmp_path / "protocol.md").write_bytes((tmp_path / "README.md").read_bytes())
    sources = {}
    for tag in c_evaluated.REFERENCES:
        source = tmp_path / ".artifacts/c-runtimes-01" / tag / "source.json"
        source.parent.mkdir(parents=True)
        source.write_text("{}")
        sources[tag] = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(c_evaluated, "verify_export", lambda record: None)
    manifest = {
        "protocol_sha256": c_evaluated.protocol_hash(),
        "tools": {"tool": "fixed"},
        "references": c_evaluated.REFERENCES,
        "cases": c_evaluated.sequence(),
        "destination": str(tmp_path / "results"),
        "sources": sources,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    (tmp_path / "release.json").write_text(
        json.dumps(
            {"approved": True, "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    )


def test_sequence_preserves_inconclusive_without_retry(tmp_path, monkeypatch):
    released_fixture(tmp_path, monkeypatch)
    seen = []

    def fake_main(folder, actions, versions, *, evaluated):
        assert evaluated
        seen.append((actions[0], versions[0]))
        folder.mkdir()
        classification = "INCONCLUSIVE" if actions[0] == "unavailable" else "PASS"
        (folder / "summary.json").write_text(
            json.dumps({"cases": [{"classification": classification}]})
        )

    monkeypatch.setattr(c_evaluated, "main", fake_main)
    c_evaluated.execute(tmp_path)
    report = json.loads((tmp_path / "results/summary.json").read_text())
    assert len(seen) == 54 and report["complete"]
    assert sum(x["classification"] == "INCONCLUSIVE" for x in report["cases"]) == 9


def test_sequence_stops_without_replacement_on_failure(tmp_path, monkeypatch):
    released_fixture(tmp_path, monkeypatch)
    seen = []

    def fake_main(folder, actions, versions, *, evaluated):
        seen.append(versions[0])
        raise RuntimeError("simulated_tool_failure")

    monkeypatch.setattr(c_evaluated, "main", fake_main)
    with pytest.raises(RuntimeError):
        c_evaluated.execute(tmp_path)
    report = json.loads((tmp_path / "results/summary.json").read_text())
    assert len(seen) == 1 and not report["complete"] and report["error_type"] == "RuntimeError"


def test_changed_protocol_blocks_before_output(tmp_path, monkeypatch):
    released_fixture(tmp_path, monkeypatch)
    (tmp_path / "README.md").write_text("changed limit")
    with pytest.raises(ValueError, match="PACKAGE_IDENTITY"):
        c_evaluated.execute(tmp_path)
    assert not (tmp_path / "results").exists()


def test_powershell_launcher_blocks_unreleased_package_from_other_cwd(tmp_path):
    import subprocess
    from pathlib import Path

    package = tmp_path / "package with spaces"
    package.mkdir()
    (package / "manifest.json").write_text("{}")
    base = Path(__file__).resolve().parents[1]
    pwsh = Path(
        r"C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
    )
    result = subprocess.run(
        [
            str(pwsh),
            "-NoProfile",
            "-File",
            str(base / "Invoke-CEvaluated.ps1"),
            "-Package",
            str(package),
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert not (tmp_path / "results").exists()

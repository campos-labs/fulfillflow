"""Frozen comparison order and active host selection without workload."""

import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest
from benchmarks import active_comparison_controls as active
from benchmarks import comparison_controls as comparison
from benchmarks import paired_controls as controls
from benchmarks import run_campaign
from benchmarks.sensitivity_controls import PWSH


@pytest.fixture
def configured(monkeypatch, tmp_path):
    for name in ("SERIES", "PACKAGE", "PROJECTS", "JOURNAL", "STEPS"):
        monkeypatch.setattr(controls, name, getattr(controls, name))
    monkeypatch.setattr(comparison, "RELEASE", comparison.RELEASE)
    monkeypatch.setenv("FULFILLFLOW_COMPARISON_ACTIVE", "1")
    monkeypatch.setattr(controls, "SERIES", "historical")
    active.configure()
    monkeypatch.setattr(controls, "JOURNAL", tmp_path / "journal")
    return controls.STEPS


@pytest.mark.parametrize("failure", [0, 7, 12])
def test_fixed_sixty_sequence_stops_at_first_failure(configured, monkeypatch, failure):
    assert len(configured) == 12
    assert [(s.profile, s.users) for s in configured[::2]] == [
        ("mixed", 4),
        ("mixed", 12),
        ("timeline", 4),
        ("timeline", 12),
        ("ingestion", 4),
        ("ingestion", 12),
    ]
    assert [s.version for s in configured] == ["v10", "v11", "v11", "v10"] * 3
    assert len({s.attempt for s in configured}) == 12
    monkeypatch.setattr(comparison, "verify_release", lambda: {})
    monkeypatch.setattr(controls, "require_new_execution", lambda: None)
    monkeypatch.setattr(controls, "assert_projects_absent", lambda: None)
    monkeypatch.setattr(controls, "verify_checksums", lambda *a: None)
    monkeypatch.setattr(controls, "read_json", lambda *a: {"launcher": {}})
    monkeypatch.setattr(comparison, "_sha256", lambda *a: "hash")
    monkeypatch.setattr(comparison, "_write_checksums", lambda *a: None)
    seen = []
    reviewed = []

    def run(step, launcher):
        seen.extend((step.number, n) for n in range(1, 6))
        return 2 if step.number == failure else 0

    monkeypatch.setattr(controls, "run_step", run)
    monkeypatch.setattr(comparison, "verify_block", lambda s: reviewed.append(s.number))
    assert comparison.execute({}) == (2 if failure else 0)
    assert len(seen) == (failure or 12) * 5
    assert reviewed == list(range(1, failure or 13))
    assert all(
        "active_comparison_controls" in " ".join(controls.preparation_argv(s)) for s in configured
    )


def test_active_policy_is_explicit_and_failure_is_not_ignored(monkeypatch):
    monkeypatch.delenv("FULFILLFLOW_COMPARISON_ACTIVE", raising=False)
    assert not run_campaign.requires_active_host(object())
    monkeypatch.setenv("FULFILLFLOW_COMPARISON_ACTIVE", "1")
    assert run_campaign.requires_active_host(object())

    def failed():
        raise RuntimeError("environment unavailable")

    monkeypatch.setattr(active, "require_energy", failed)
    with pytest.raises(RuntimeError, match="environment unavailable"):
        active.verify_host_policy()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell required")
@pytest.mark.parametrize("fail_at", [0, 17, -1])
def test_real_powershell_sixty_simulated_repetitions(tmp_path, fail_at):
    root = tmp_path / "path with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    for name in ("Invoke-ActiveComparison.ps1", "ActiveScreenIO.ps1", "ActiveScreenIO.cs"):
        shutil.copyfile(source / "scripts" / name, scripts / name)
    (scripts / "ActiveScreenGuard.cs").write_text("""
using System;
public class ActiveScreenGuard : IDisposable {
 public bool Ready=true,Released=false; public string Failure=""; public int Display=1;
 public long HeartbeatTicks { get { return DateTime.UtcNow.Ticks; } }
 public string[] Events(){return new string[]{"simulated"};}
 public void Dispose(){Released=true;}
}""")
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    module = root / "benchmarks"
    module.mkdir()
    (module / "__init__.py").write_text("")
    (module / "active_comparison_controls.py").write_text(
        "from pathlib import Path\nimport sys\n"
        f"if '--preflight' in sys.argv: sys.exit({2 if fail_at == -1 else 0})\n"
        "for n in range(1,61):\n"
        " with Path('sequence.txt').open('a') as f: f.write(str(n)+'\\n')\n"
        f" if n=={fail_at}:sys.exit(2)\n"
    )
    release = module / "results/comparison-active-release-03"
    release.mkdir(parents=True)
    (release / "ready.json").write_text("{}")
    args = [str(PWSH), "-NoProfile", "-File", str(scripts / "Invoke-ActiveComparison.ps1")]
    r = subprocess.run(args, cwd=tmp_path, capture_output=True, timeout=30)
    assert r.returncode == (2 if fail_at else 0), r.stderr
    if fail_at == -1:
        assert not (module / "results/comparison-active-operation-03").exists()
        assert not (root / "sequence.txt").exists()
        return
    assert len((root / "sequence.txt").read_text().splitlines()) == (fail_at or 60)
    report = module / "results/comparison-active-operation-03/result.json"
    before = report.read_bytes()
    assert json.loads(before)["released"] is True
    r = subprocess.run(args, cwd=tmp_path, capture_output=True, timeout=30)
    assert r.returncode == 2 and report.read_bytes() == before

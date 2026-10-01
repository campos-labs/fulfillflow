"""Provenance rejection cases without application execution or external services."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from validation.functional import provenance


@dataclass
class Distribution:
    name: str
    version: str

    @property
    def metadata(self) -> dict[str, str]:
        return {"Name": self.name}


@dataclass
class SourceFixture:
    source: Path
    folder: Path
    answers: dict[tuple[str, ...], str]
    installed: list[Distribution]
    requirements: dict[str, list[str]]


@pytest.fixture
def frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SourceFixture:
    source = tmp_path / "frozen source"
    folder = source / "validation" / "functional"
    folder.mkdir(parents=True)
    module = source / "src" / "fulfillflow" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text('"""Frozen application identity fixture."""\n', encoding="utf-8")
    (source / "uv.lock").write_text(
        '[[package]]\nname="alpha"\nversion="1.0"\n[[package]]\nname="beta"\nversion="2.0"\n',
        encoding="utf-8",
    )
    (source / "pyproject.toml").write_text(
        '[project]\ndependencies=["alpha==1.0"]\n', encoding="utf-8"
    )
    (folder / "README.md").write_text("Protocol fixture\n", encoding="utf-8")
    (folder / "run_b.py").write_text("# Tool fixture\n", encoding="utf-8")
    answers = {
        ("rev-parse", "HEAD"): provenance.EXPECTED,
        ("rev-parse", "v1.2.0-rc.1^{commit}"): provenance.EXPECTED,
        ("diff", "--name-only", "HEAD"): "",
        ("ls-files", "--others", "--", "src"): "",
    }

    def git(_source: Path, *args: str) -> str:
        if args[0] == "ls-files" and "--others" not in args:
            return "src/fulfillflow/__init__.py\nuv.lock\npyproject.toml"
        return answers[args]

    installed = [Distribution("alpha", "1.0"), Distribution("beta", "2.0")]
    requirements = {"alpha": ["beta>=2"], "beta": []}
    fake_module = ModuleType("fulfillflow")
    fake_module.__file__ = str(module)
    monkeypatch.setitem(sys.modules, "fulfillflow", fake_module)
    monkeypatch.setattr(provenance, "git", git)
    monkeypatch.setattr(provenance.importlib.metadata, "distributions", lambda: installed)
    monkeypatch.setattr(provenance.importlib.metadata, "requires", lambda name: requirements[name])
    return SourceFixture(source, folder, answers, installed, requirements)


def test_capture_separates_application_and_tool_hashes(frozen: SourceFixture) -> None:
    evidence = provenance.capture(frozen.source, frozen.folder)
    assert evidence["application"]["sha"] == provenance.EXPECTED
    assert evidence["application"]["source"] == str(frozen.source)
    assert (
        evidence["application"]["files_sha256"]["uv.lock"]
        == hashlib.sha256((frozen.source / "uv.lock").read_bytes()).hexdigest()
    )
    assert evidence["runtime"]["production_dependencies_checked"] == ["alpha", "beta"]
    identity = evidence["tool"]["identity"]
    original_app_hashes = evidence["application"]["files_sha256"]
    (frozen.folder / "run_b.py").write_text("# Changed tool only\n", encoding="utf-8")
    changed = provenance.capture(frozen.source, frozen.folder)
    assert changed["tool"]["identity"] != identity
    assert changed["application"]["files_sha256"] == original_app_hashes
    assert "credentials" not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("key", "value", "expected_error"),
    [
        (("rev-parse", "HEAD"), "0" * 40, "WRONG_APPLICATION_HEAD"),
        (("rev-parse", "v1.2.0-rc.1^{commit}"), "0" * 40, "WRONG_APPLICATION_TAG"),
        (("diff", "--name-only", "HEAD"), "src/changed.py", "FROZEN_TRACKED_FILES_CHANGED"),
    ],
)
def test_capture_rejects_git_identity_changes(
    frozen: SourceFixture, key: tuple[str, ...], value: str, expected_error: str
) -> None:
    frozen.answers[key] = value
    with pytest.raises(ValueError, match=expected_error):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_rejects_import_from_different_checkout(frozen: SourceFixture) -> None:
    sys.modules["fulfillflow"].__file__ = str(frozen.source.parent / "other" / "__init__.py")
    with pytest.raises(ValueError, match="APPLICATION_IMPORT_SOURCE_MISMATCH"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_rejects_missing_transitive_production_dependency(frozen: SourceFixture) -> None:
    frozen.installed.pop()
    with pytest.raises(ValueError, match="PRODUCTION_DEPENDENCY_NOT_FROZEN"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_rejects_installed_version_different_from_lock(frozen: SourceFixture) -> None:
    frozen.installed[0].version = "9.0"
    with pytest.raises(ValueError, match="INSTALLED_VERSION_DIFFERS_FROM_LOCK"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_rejects_lock_version_outside_dependency_requirement(frozen: SourceFixture) -> None:
    (frozen.source / "pyproject.toml").write_text(
        '[project]\ndependencies=["alpha>=2"]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="DEPENDENCY_REQUIREMENT_NOT_MET"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_rejects_missing_dependency_requested_by_extra(frozen: SourceFixture) -> None:
    (frozen.source / "pyproject.toml").write_text(
        '[project]\ndependencies=["alpha[binary]==1.0"]\n', encoding="utf-8"
    )
    frozen.requirements["alpha"] = ['beta>=2; extra == "binary"']
    frozen.installed.pop()
    with pytest.raises(ValueError, match="PRODUCTION_DEPENDENCY_NOT_FROZEN"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_checks_extra_when_package_was_already_seen_without_extra(
    frozen: SourceFixture,
) -> None:
    (frozen.source / "pyproject.toml").write_text(
        '[project]\ndependencies=["alpha[binary]==1.0", "alpha==1.0"]\n', encoding="utf-8"
    )
    frozen.requirements["alpha"] = ['beta>=2; extra == "binary"']
    frozen.installed.pop()
    with pytest.raises(ValueError, match="PRODUCTION_DEPENDENCY_NOT_FROZEN"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_ignores_generated_artifacts_in_tool_identity(frozen: SourceFixture) -> None:
    initial = provenance.capture(frozen.source, frozen.folder)["tool"]["identity"]
    for directory in (".artifacts", "results", "__pycache__", ".pytest_cache"):
        generated = frozen.folder / directory
        generated.mkdir()
        (generated / "ignored.bin").write_bytes(b"generated data")
    after = provenance.capture(frozen.source, frozen.folder)
    assert after["tool"]["identity"] == initial
    assert set(after["tool"]["files_sha256"]) == {"README.md", "run_b.py"}


def test_capture_rejects_untracked_application_module(frozen: SourceFixture) -> None:
    frozen.answers[("ls-files", "--others", "--", "src")] = "src/fulfillflow/injected.py"
    with pytest.raises(ValueError, match="UNTRACKED_APPLICATION_MODULE"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_rejects_installed_package_absent_from_lock(frozen: SourceFixture) -> None:
    frozen.installed.append(Distribution("unlocked", "1.0"))
    with pytest.raises(ValueError, match="INSTALLED_PACKAGE_NOT_IN_LOCK"):
        provenance.capture(frozen.source, frozen.folder)


def test_capture_normalizes_distribution_name_dot_underscore_and_hyphen(
    frozen: SourceFixture,
) -> None:
    with (frozen.source / "uv.lock").open("a", encoding="utf-8") as stream:
        stream.write('[[package]]\nname="zope-interface"\nversion="8.6"\n')
    frozen.installed.append(Distribution("zope.interface", "8.6"))
    assert (
        provenance.capture(frozen.source, frozen.folder)["runtime"]["installed"]["zope-interface"]
        == "8.6"
    )

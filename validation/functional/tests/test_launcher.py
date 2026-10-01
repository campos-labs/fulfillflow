"""Exercise the real PowerShell entry point against a harmless Python child."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PWSH = Path(
    r"C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime"
    r"\dependencies\native\powershell\pwsh.exe"
)
FOLDER = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "checkout with spaces"
    folder = source / "validation" / "functional"
    folder.mkdir(parents=True)
    foreign = tmp_path / "foreign directory"
    foreign.mkdir()
    copied = folder / "Invoke-Functional.ps1"
    shutil.copyfile(FOLDER / copied.name, copied)
    (folder / "run_b.py").write_text(
        "import json, os, pathlib, sys\n"
        "folder = pathlib.Path(__file__).resolve().parent\n"
        "marker = folder / 'invocations.txt'\n"
        "with marker.open('a', encoding='utf-8') as stream:\n"
        "    stream.write('invoked\\n')\n"
        "(folder / 'observed.json').write_text(json.dumps({\n"
        "    'argv': sys.argv[1:], 'cwd': str(pathlib.Path.cwd()),\n"
        "    'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "    'bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE'),\n"
        "    'dont_write_bytecode': sys.dont_write_bytecode,\n"
        "}), encoding='utf-8')\n"
        "raise SystemExit(int(os.environ.get('FUNCTIONAL_FAKE_EXIT', '0')))\n",
        encoding="utf-8",
    )
    return source, copied, foreign


def invoke(
    script: Path, cwd: Path, extra: list[str], exit_code: int = 0
) -> subprocess.CompletedProcess[str]:
    assert PWSH.is_file(), "The explicitly configured PowerShell executable must exist."
    environment = dict(os.environ)
    environment.update(
        {
            "FUNCTIONAL_FAKE_EXIT": str(exit_code),
            "PYTHONPATH": "inherited-path-must-not-be-used",
            "PYTHONDONTWRITEBYTECODE": "0",
        }
    )
    return subprocess.run(
        [str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script), *extra],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


@pytest.mark.parametrize("exit_code", [0, 7])
def test_launcher_preserves_child_exit_and_invokes_once_from_foreign_cwd(
    launcher: tuple[Path, Path, Path], exit_code: int
) -> None:
    source, script, foreign = launcher
    result = invoke(
        script,
        foreign,
        [
            "-RunId",
            "focal-launcher-01",
            "-Mode",
            "Development",
            "-Owner",
            "tracking",
            "-Repetitions",
            "2",
            "-PythonExecutable",
            sys.executable,
        ],
        exit_code,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    observed = json.loads((script.parent / "observed.json").read_text(encoding="utf-8"))
    assert observed["argv"] == [
        "--source",
        str(source),
        "--run-id",
        "focal-launcher-01",
        "--mode",
        "development",
        "--owner",
        "tracking",
        "--repetitions",
        "2",
    ]
    assert observed["cwd"] == str(foreign)
    assert observed["pythonpath"] == str(source / "src") + os.pathsep + str(script.parent)
    assert observed["bytecode"] == "1"
    assert observed["dont_write_bytecode"] is True
    assert (script.parent / "invocations.txt").read_text(encoding="utf-8") == "invoked\n"
    if exit_code:
        assert "no automatic retry" in result.stdout
        assert f"exit code {exit_code}" in result.stdout
    else:
        assert "stopped" not in result.stdout


@pytest.mark.parametrize("python_path", ["python.exe", r"C:\nonexistent\python.exe"])
def test_launcher_rejects_nonabsolute_or_missing_python_without_child(
    launcher: tuple[Path, Path, Path], python_path: str
) -> None:
    _source, script, foreign = launcher
    result = invoke(script, foreign, ["-RunId", "focal-01", "-PythonExecutable", python_path])
    assert result.returncode != 0
    assert "existing absolute path" in result.stderr
    assert not (script.parent / "invocations.txt").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["-RunId", "../outside"],
        ["-RunId", "focal-01", "-Repetitions", "0"],
        ["-RunId", "focal-01", "-Repetitions", "4"],
        ["-RunId", "focal-01", "-Owner", "external"],
    ],
)
def test_launcher_rejects_invalid_parameters_without_child(
    launcher: tuple[Path, Path, Path], arguments: list[str]
) -> None:
    _source, script, foreign = launcher
    result = invoke(script, foreign, [*arguments, "-PythonExecutable", sys.executable])
    assert result.returncode != 0
    assert not (script.parent / "invocations.txt").exists()


@pytest.mark.parametrize("subnet", ["10.254.240.0/28", ""])
def test_launcher_forwards_explicit_subnet_without_selection_or_retry(
    launcher: tuple[Path, Path, Path],
    subnet: str,
) -> None:
    _source, script, foreign = launcher
    result = invoke(
        script,
        foreign,
        ["-RunId", "subnet-01", "-PythonExecutable", sys.executable, "-NetworkSubnet", subnet],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((script.parent / "observed.json").read_text(encoding="utf-8"))
    if subnet:
        assert observed["argv"][-2:] == ["--network-subnet", subnet]
    else:
        assert "--network-subnet" not in observed["argv"]
    assert (script.parent / "invocations.txt").read_text(encoding="utf-8") == "invoked\n"


def test_launcher_forwards_release_with_spaces(launcher: tuple[Path, Path, Path]) -> None:
    _source, script, foreign = launcher
    release = str(foreign / "reviewed release.json")
    digest = "a" * 64
    result = invoke(
        script,
        foreign,
        [
            "-RunId",
            "released-01",
            "-PythonExecutable",
            sys.executable,
            "-Mode",
            "Evaluated",
            "-ReleaseFile",
            release,
            "-ReleaseSha256",
            digest,
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((script.parent / "observed.json").read_text())
    assert observed["argv"][-4:] == ["--release-file", release, "--release-sha256", digest]

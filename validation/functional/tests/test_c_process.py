"""Verify a real Windows descendant dies at outer timeout."""

import os
from pathlib import Path

import pytest
from validation.functional.c_process import run_owned


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object qualification")
@pytest.mark.parametrize("tag", ["v1.0.0", "v1.1.0-rc.1", "v1.2.0-rc.1"])
def test_native_tree_timeout(tmp_path, tag):
    executable = (
        Path(__file__).resolve().parents[1]
        / ".artifacts/c-runtimes-01"
        / tag
        / "venv/Scripts/python.exe"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    pidfile = tmp_path / "descendant.txt"
    result = run_owned(
        [
            str(executable),
            "-B",
            "-m",
            "validation.functional.qualification.process_tree_fixture",
            "tree",
            str(pidfile),
        ],
        cwd=Path.cwd(),
        env=env,
        timeout=3,
    )
    assert Path(result["startup"]["prefix"]).resolve() == executable.parent.parent
    assert result["startup"]["pid"] == result["pid"]
    assert pidfile.exists()
    assert result["timed_out"] and result["tree_empty"] and result["forced_tree_cleanup"]
    assert result["exit_code"] != 0


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object qualification")
@pytest.mark.parametrize("tag", ["v1.0.0", "v1.1.0-rc.1", "v1.2.0-rc.1"])
def test_native_nonzero_is_preserved(tag):
    executable = (
        Path(__file__).resolve().parents[1]
        / ".artifacts/c-runtimes-01"
        / tag
        / "venv/Scripts/python.exe"
    )
    result = run_owned(
        [
            str(executable),
            "-B",
            "-m",
            "validation.functional.qualification.process_tree_fixture",
            "exit",
        ],
        cwd=Path.cwd(),
        env=os.environ.copy(),
        timeout=10,
    )
    assert result["exit_code"] == 2 and not result["timed_out"] and result["tree_empty"]
    assert not result["forced_tree_cleanup"]

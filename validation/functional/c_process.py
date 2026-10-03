"""Windows-owned process tree with a start gate and verified bounded termination."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import time
from ctypes import wintypes as w
from pathlib import Path
from typing import Any


class Accounting(ctypes.Structure):
    _fields_ = [
        (n, ctypes.c_longlong) for n in ("user", "kernel", "period_user", "period_kernel")
    ] + [(n, w.DWORD) for n in ("faults", "total", "active", "terminated")]


class BasicLimit(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", w.DWORD),
        ("min_ws", ctypes.c_size_t),
        ("max_ws", ctypes.c_size_t),
        ("active_limit", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimit),
        ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


def interpreter_identity(executable: str, env: dict[str, str]) -> dict[str, Any]:
    probe = subprocess.run(
        [
            executable,
            "-I",
            "-B",
            "-c",
            (
                "import sys,json; print(json.dumps(dict(native=sys._base_executable,"
                "prefix=sys.prefix,version=list(sys.version_info[:3]))))"
            ),
        ],
        env=env,
        capture_output=True,
        timeout=10,
        check=True,
    )
    identity = json.loads(probe.stdout)
    native = Path(identity["native"]).resolve()
    virtual = Path(executable).resolve()
    if (
        identity["version"] != [3, 13, 1]
        or not native.is_file()
        or Path(identity["prefix"]).resolve() != virtual.parent.parent
    ):
        raise ValueError("UNQUALIFIED_INTERPRETER")
    return {
        "native": str(native),
        "virtual": str(virtual),
        "prefix": identity["prefix"],
        "native_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
        "virtual_sha256": hashlib.sha256(virtual.read_bytes()).hexdigest(),
    }


def run_owned(args: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("WINDOWS_JOB_REQUIRED")
    identity = interpreter_identity(args[0], env)
    launch_env = dict(env, __PYVENV_LAUNCHER__=identity["virtual"])
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, params, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
        ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
        (
            "QueryInformationJobObject",
            [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p],
            w.BOOL,
        ),
        ("CloseHandle", [w.HANDLE], w.BOOL),
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
    ):
        fn = getattr(k, name)
        fn.argtypes = params
        fn.restype = result
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    child = None

    def active() -> int:
        data = Accounting()
        if not k.QueryInformationJobObject(job, 1, ctypes.byref(data), ctypes.sizeof(data), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(data.active)

    try:
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000
        if not k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        # The bootstrap does not run the case or spawn descendants before assignment.
        bootstrap = (
            "import sys,runpy,json,os; assert sys.stdin.readline()=='GO\\n'; "
            "print(json.dumps(dict(pid=os.getpid(),prefix=sys.prefix)),flush=True); "
            "sys.argv=sys.argv[1:]; runpy.run_module(sys.argv[0],run_name='__main__')"
        )
        if args[1:3] != ["-B", "-m"]:
            raise ValueError("MODULE_INVOCATION_REQUIRED")
        child = subprocess.Popen(
            [identity["native"], "-B", "-c", bootstrap, *args[3:]],
            cwd=cwd,
            env=launch_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        process_handle = k.OpenProcess(0x0101, False, child.pid)
        if not process_handle:
            child.kill()
            child.wait(timeout=10)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k.AssignProcessToJobObject(job, process_handle):
                error = ctypes.get_last_error()
                child.kill()
                child.wait(timeout=10)
                raise ctypes.WinError(error)
        finally:
            k.CloseHandle(process_handle)
        started = time.monotonic()
        timed_out = False
        try:
            output, _ = child.communicate(b"GO\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if not k.TerminateJobObject(job, 2):
                raise ctypes.WinError(ctypes.get_last_error()) from None
            output, _ = child.communicate(timeout=10)
        settle_deadline = time.monotonic() + 1
        while active() and time.monotonic() < settle_deadline:
            time.sleep(0.01)
        remaining = active()
        forced = timed_out or remaining > 0
        if remaining and not k.TerminateJobObject(job, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 10
        while active():
            if time.monotonic() >= deadline:
                raise RuntimeError("PROCESS_TREE_NOT_EMPTY")
            time.sleep(0.05)
        startup = json.loads(output.splitlines()[0])
        if (
            startup["pid"] != child.pid
            or Path(startup["prefix"]).resolve() != Path(identity["prefix"]).resolve()
        ):
            raise ValueError("NATIVE_PID_OR_VENV_MISMATCH")
        return {
            "interpreter": identity,
            "startup": startup,
            "exit_code": child.returncode,
            "timed_out": timed_out,
            "tree_empty": True,
            "forced_tree_cleanup": forced,
            "pid": child.pid,
            "seconds": time.monotonic() - started,
            "console_sha256": hashlib.sha256(output).hexdigest(),
        }
    finally:
        if child is not None and child.poll() is None:
            k.TerminateJobObject(job, 2)
            child.wait(timeout=10)
        k.CloseHandle(job)

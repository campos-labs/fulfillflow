"""Fail-closed host heartbeat checks, exclusive to the active-screen diagnostic."""

import ctypes
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


class EnergyConditionError(RuntimeError):
    pass


def read_heartbeat(path: str) -> str:
    """Share deletion with the atomic publisher; never retry or reuse a stale read."""
    if sys.platform != "win32":
        return Path(path).read_text(encoding="utf-8-sig")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    name = (
        "Local\\FulfillFlowHeartbeat"
        + hashlib.sha256(os.path.abspath(path).lower().encode("utf-8")).hexdigest()
    )
    mutex = create_mutex(None, False, name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    release = kernel.ReleaseMutex
    release.argtypes = [wintypes.HANDLE]
    result = wait(mutex, 250)
    native_error = ctypes.get_last_error() if result == 0xFFFFFFFF else None
    if result != 0:
        if result == 0x80:
            release(mutex)
        close(mutex)
        raise EnergyConditionError(
            f"heartbeat mutex unavailable; native_wait={result}; winerror={native_error}"
        )
    try:
        return _read_windows_file(path)
    finally:
        release(mutex)
        close(mutex)


def _read_windows_file(path: str) -> str:
    # Kept inside the mutex from open through decoding.
    if sys.platform != "win32":
        raise EnergyConditionError("Windows reader required")
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(path, 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    with os.fdopen(fd, "r", encoding="utf-8-sig") as stream:
        return stream.read()


def require_energy() -> None:
    path = os.environ.get("FULFILLFLOW_ACTIVE_GUARD")
    if not path:
        raise EnergyConditionError("active-screen observer is required")
    try:
        state = json.loads(read_heartbeat(path))
        age = (datetime.now(UTC) - datetime.fromisoformat(state["heartbeat_utc"])).total_seconds()
        if (
            state["ready"] is not True
            or state["failure"] != ""
            or state["display"] != 1
            or not 0 <= age <= 3
        ):
            raise EnergyConditionError("screen/session/AC condition or observer freshness failed")
    except OSError as exc:
        raise EnergyConditionError(
            f"heartbeat read failed: {type(exc).__name__}; errno={exc.errno}; "
            f"winerror={getattr(exc, 'winerror', None)}"
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise EnergyConditionError(f"heartbeat format failed: {type(exc).__name__}") from exc

"""Protected local cookie jar for explicit browser-authenticated tools."""

import json
import os
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit


def write_private_json(path: Path, data: object, *, replace: bool = False) -> None:
    """Protect only this file, before writing credentials. Publish a complete copy."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as staging:
        temporary = Path(staging.name)
    try:
        if os.name == "nt":
            account = subprocess.check_output(["whoami"], text=True).strip()
            subprocess.run(
                ["icacls", str(temporary), "/inheritance:r", "/grant:r", f"{account}:F"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        temporary.write_text(json.dumps(data), encoding="utf-8")
        if replace:
            temporary.replace(path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_session(path: Path, base_url: str, result: dict[str, Any]) -> None:
    token = next(c["value"] for c in result["cookies"] if c["name"] == "fulfillflow_session")
    write_private_json(
        path, {"origin": base_url.rstrip("/"), "cookie": token, **result["session"]}, replace=True
    )


def session_headers(path: Path, base_url: str) -> dict[str, str]:
    record = json.loads(path.read_text(encoding="utf-8"))
    expected, actual = urlsplit(record["origin"]), urlsplit(base_url)
    if (expected.scheme, expected.netloc) != (actual.scheme, actual.netloc):
        raise ValueError("Session belongs to another application origin")
    return {
        "Cookie": "fulfillflow_session=" + record["cookie"],
        "X-CSRF-Token": record["csrf_token"],
    }

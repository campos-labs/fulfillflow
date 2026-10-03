"""Package selected existing records; never execute scenarios or modify originals."""

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project(data: bytes) -> bytes:
    text = data.decode("utf-8-sig")
    # Same deterministic projection for JSON-escaped and ordinary Windows paths.
    text = re.sub(r"C:(?:\\+|/)Users(?:\\+|/)natoc", "<USER_HOME>", text, flags=re.I)
    text = re.sub(r"C:(?:\\+|/)Projetos", "<PROJECTS>", text, flags=re.I)
    return text.encode("utf-8")


def build(base: Path, output: Path) -> None:
    output.mkdir(exist_ok=False)
    run = base / "results/c-evaluated-01"
    selected = {"evaluated.zip": sorted(p for p in run.rglob("*") if p.is_file())}
    preparation: list[Path] = []
    for name in (
        "c-evaluated-preparation-02",
        "c-evaluated-review-01",
        "c-native-runtime-review-01",
        "c-outage-matrix-review-01",
    ):
        preparation.extend(p for p in (base / ".artifacts" / name).iterdir() if p.is_file())
    for name in ("c-final-tests-02.xml", "c-final-policy-tests-03.xml"):
        preparation.append(base / ".artifacts" / name)
    for tag in ("v1.0.0", "v1.1.0-rc.1", "v1.2.0-rc.1"):
        for name in ("source.json", "runtime.json"):
            preparation.append(base / ".artifacts/c-runtimes-01" / tag / name)
    selected["preparation.zip"] = sorted(preparation)
    manifest: dict[str, Any] = {
        "schema": 1,
        "representation": "UTF-8 copies; BOM removed; local user/project prefixes replaced",
        "originals_unchanged": True,
        "selection": "Complete evaluated directory; selected preparation/review/runtime records",
        "excluded": [
            "runtime installations",
            "frozen source trees",
            "volumes and images",
            "earlier development attempts except consolidated C4 review",
            "B records available separately; not counted as C",
        ],
        "archives": {},
    }
    for name, files in selected.items():
        entries = []
        with zipfile.ZipFile(output / name, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                original = path.read_bytes()
                projected = project(original)
                relative = path.relative_to(base).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 0
                info.external_attr = 0x20
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, projected)
                entries.append(
                    {
                        "path": relative,
                        "original_sha256": sha(original),
                        "distributed_sha256": sha(projected),
                        "changed": original != projected,
                    }
                )
        manifest["archives"][name] = {
            "sha256": sha((output / name).read_bytes()),
            "entries": entries,
        }
    (output / "packages.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({name: len(value["entries"]) for name, value in manifest["archives"].items()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.base.resolve(), args.output.resolve())

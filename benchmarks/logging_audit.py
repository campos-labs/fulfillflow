"""Inspect frozen logging configuration without starting application servers."""

from __future__ import annotations

import json
from typing import Any

from benchmarks.collectors import ExternalCommandError, run_capture

_SCRIPT = r"""
import hashlib, importlib.metadata, json, logging, sys
from pathlib import Path
import fulfillflow, uvicorn
root = Path(fulfillflow.__file__).parent
config = uvicorn.Config('fulfillflow.main:app', workers=1)
def stream_name(handler):
    stream = getattr(handler, 'stream', None)
    return 'stdout' if stream is sys.stdout else 'stderr' if stream is sys.stderr else 'other'
def logger(name):
    item = logging.getLogger(name)
    return {'level': logging.getLevelName(item.getEffectiveLevel()), 'propagate': item.propagate,
            'handlers': [{'stream': stream_name(h),
                          'format': getattr(h.formatter, '_fmt', None)} for h in item.handlers]}
references = []
for path in root.rglob('*.py'):
    words = ('logging', 'log_level', 'log_format', 'prometheus', 'opentelemetry')
    if any(word in path.read_text() for word in words):
        references.append(path.relative_to(root).as_posix())
print(json.dumps({'uvicorn_version': importlib.metadata.version('uvicorn'),
    'access_log': config.access_log, 'log_level_argument': config.log_level,
    'root': logger(''), 'loggers': {name: logger(name)
        for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'fulfillflow')},
    'observability_references': sorted(references),
    'entrypoints': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob('__main__.py')}}))
"""


def validate_policy(report: dict[str, Any]) -> None:
    """Validate observed defaults; environment declarations alone are insufficient."""
    if not (
        report["uvicorn_version"] == "0.52.4"
        and report["access_log"] is True
        and report["log_level_argument"] is None
        and report["root"]["level"] == "WARNING"
        and report["root"]["handlers"] == []
        and report["observability_references"] == ["config.py"]
    ):
        raise ExternalCommandError("frozen application logging configuration differs")
    for name, level, streams, propagate in (
        ("uvicorn", "INFO", ["stderr"], False),
        ("uvicorn.error", "INFO", [], True),
        ("uvicorn.access", "INFO", ["stdout"], False),
        ("fulfillflow", "WARNING", [], True),
    ):
        logger = report["loggers"][name]
        if (
            logger["level"] != level
            or [item["stream"] for item in logger["handlers"]] != streams
            or logger["propagate"] is not propagate
        ):
            raise ExternalCommandError("frozen server logging policy differs")
    if report["loggers"]["uvicorn"]["handlers"][0]["format"] != "%(levelprefix)s %(message)s":
        raise ExternalCommandError("frozen server log format differs")
    if report["loggers"]["uvicorn.access"]["handlers"][0]["format"] != (
        '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    ):
        raise ExternalCommandError("frozen access log format differs")


def audit_image(image: str) -> dict[str, Any]:
    completed = run_capture(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python",
            image,
            "-B",
            "-c",
            _SCRIPT,
        ],
        30,
    )
    report: dict[str, Any] = json.loads(completed.stdout)
    validate_policy(report)
    return report

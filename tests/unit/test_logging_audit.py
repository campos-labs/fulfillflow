"""Keep observed logging policy distinct from environment declarations."""

import copy

import pytest
from benchmarks.collectors import ExternalCommandError
from benchmarks.logging_audit import validate_policy


def policy():
    return {
        "uvicorn_version": "0.52.4",
        "access_log": True,
        "log_level_argument": None,
        "root": {"level": "WARNING", "handlers": []},
        "observability_references": ["config.py"],
        "loggers": {
            "uvicorn": {
                "level": "INFO",
                "propagate": False,
                "handlers": [{"stream": "stderr", "format": "%(levelprefix)s %(message)s"}],
            },
            "uvicorn.error": {"level": "INFO", "propagate": True, "handlers": []},
            "uvicorn.access": {
                "level": "INFO",
                "propagate": False,
                "handlers": [
                    {
                        "stream": "stdout",
                        "format": '%(levelprefix)s %(client_addr)s - "%(request_line)s" '
                        "%(status_code)s",
                    }
                ],
            },
            "fulfillflow": {"level": "WARNING", "propagate": True, "handlers": []},
        },
    }


def test_observed_frozen_policy_is_accepted():
    validate_policy(policy())


@pytest.mark.parametrize(
    "change", ["access", "level", "stream", "format", "root", "emitter", "propagate", "server"]
)
def test_declarations_cannot_legitimize_effective_policy_drift(change):
    report = copy.deepcopy(policy())
    report.update(LOG_LEVEL="WARNING", LOG_FORMAT="json")
    if change == "access":
        report["access_log"] = False
    elif change == "level":
        report["loggers"]["uvicorn.access"]["level"] = "WARNING"
    elif change == "stream":
        report["loggers"]["uvicorn.access"]["handlers"][0]["stream"] = "stderr"
    elif change == "format":
        report["loggers"]["uvicorn.access"]["handlers"][0]["format"] = "json"
    elif change == "root":
        report["root"]["handlers"] = [{"stream": "stdout"}]
    elif change == "propagate":
        report["loggers"]["uvicorn.error"]["propagate"] = False
    elif change == "server":
        report["loggers"]["uvicorn"]["handlers"][0]["format"] = "json"
    else:
        report["observability_references"].append("business_logging.py")
    with pytest.raises(ExternalCommandError):
        validate_policy(report)

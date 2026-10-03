"""Ownership rejection before any C4 destructive transition."""

import copy

import pytest
from validation.functional.c_dependency import check_owner, transition


def owned():
    return {
        "Id": "exact-id",
        "State": {"Running": True},
        "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5555"}]}},
        "Config": {"Labels": {"org.fulfillflow.c.owner": "ff-c-tcp-own"}},
        "HostConfig": {"PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5555"}]}},
    }


def test_owned_dependency_matches():
    check_owner(owned(), "exact-id", "ff-c-tcp-own", "5555")


@pytest.mark.parametrize("field", ["id", "label", "ip", "port"])
def test_mismatched_dependency_rejected(field):
    data = copy.deepcopy(owned())
    if field == "id":
        data["Id"] = "other"
    if field == "label":
        data["Config"]["Labels"]["org.fulfillflow.c.owner"] = "other"
    if field == "ip":
        data["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    if field == "port":
        data["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = "9999"
    with pytest.raises(ValueError, match="OWNERSHIP"):
        check_owner(data, "exact-id", "ff-c-tcp-own", "5555")


def test_invalid_transition_never_calls_docker(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not execute")

    monkeypatch.setattr("validation.functional.c_dependency.subprocess.run", forbidden)
    with pytest.raises(ValueError, match="INVALID_DEPENDENCY_ACTION"):
        transition("id", "ff-c-tcp-own", "5555", "remove")


def test_dynamic_port_requires_matching_effective_binding():
    data = owned()
    data["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = ""
    check_owner(data, "exact-id", "ff-c-tcp-own", "5555")
    data["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"] = "9999"
    with pytest.raises(ValueError, match="EFFECTIVE_PORT"):
        check_owner(data, "exact-id", "ff-c-tcp-own", "5555")
    data["State"]["Running"] = False
    data["NetworkSettings"]["Ports"] = {}
    check_owner(data, "exact-id", "ff-c-tcp-own", "5555")

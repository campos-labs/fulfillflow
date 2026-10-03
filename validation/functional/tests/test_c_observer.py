"""Observe completion, including exception and in-flight internal request cases."""

import asyncio

import pytest
from validation.functional.c_observer import CompletionObserver


@pytest.mark.asyncio
async def test_response_started_is_not_request_finished():
    release = asyncio.Event()
    started = asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503})
        started.set()
        await release.wait()

    observer = CompletionObserver(app, "secret")
    sent = []

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(observer({"type": "http", "path": "/business"}, None, send))
    await started.wait()
    assert observer.active == 1 and observer.finished == 0
    await observer(
        {"type": "http", "path": "/__c_observer", "headers": [(b"x-c-observer", b"secret")]},
        None,
        send,
    )
    assert observer.active == 1 and observer.started == 1
    release.set()
    await task
    assert observer.active == 0 and observer.finished == 1 and observer.last_status == 503


@pytest.mark.asyncio
async def test_exception_does_not_invent_response():
    async def app(*args):
        raise RuntimeError("not exported")

    observer = CompletionObserver(app, "secret")
    with pytest.raises(RuntimeError):
        await observer({"type": "http", "path": "/business"}, None, None)
    assert observer.active == 0 and observer.finished == 1 and observer.last_status is None


@pytest.mark.asyncio
async def test_observer_requires_token_without_calling_application():
    async def app(*args):
        raise AssertionError("must not call")

    observer = CompletionObserver(app, "secret")
    sent = []

    async def send(message):
        sent.append(message)

    await observer({"type": "http", "path": "/__c_observer"}, None, send)
    assert sent[0]["status"] == 403 and observer.started == 0


def test_request_identity_and_internal_completion_required():
    from validation.functional.c_observer import request_finished

    target = "target"
    public = {"request_id": target, "kind": "public_webhook", "finished": True}
    internal = {"request_id": target, "kind": "internal_or_other", "finished": False}
    states = {
        "core": {"active": 0, "requests": [public]},
        "tracking": {"active": 1, "requests": [internal]},
    }
    assert not request_finished(states, target)
    internal["finished"] = True
    states["tracking"]["active"] = 0
    assert request_finished(states, target)
    assert not request_finished(states, "unrelated")
    public["finished"] = False
    assert not request_finished(states, target)


@pytest.mark.asyncio
async def test_observer_preserves_valid_request_id_and_status():
    request_id = "bb418709-4010-494b-9ab3-968332e3c594"

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503})

    async def send(message):
        pass

    observer = CompletionObserver(app, "token")
    await observer(
        {
            "type": "http",
            "path": "/api/v1/carriers/alpha/events",
            "headers": [(b"x-request-id", request_id.encode())],
        },
        None,
        send,
    )
    assert observer.requests == [
        {
            "request_id": request_id,
            "sequence": 1,
            "kind": "public_webhook",
            "finished": True,
            "status": 503,
        }
    ]

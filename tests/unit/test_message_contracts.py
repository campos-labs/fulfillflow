import json
from datetime import timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from tests.message_support import command_message, result_message

from fulfillflow.contracts.messages import CommandEnvelope, decode_message, encode_message


@pytest.mark.parametrize("message", [command_message(), result_message()])
def test_canonical_roundtrip(message):
    assert decode_message(encode_message(message)) == message
    assert encode_message(decode_message(encode_message(message))) == encode_message(message)
    assert b"raw_body" not in encode_message(message)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("type", "unknown"),
        ("payload_sha256", "b" * 64),
        ("event_id", str(UUID(int=8))),
        ("unexpected", True),
    ],
)
def test_invalid_envelope(field, value):
    body = command_message().model_dump(mode="json")
    body[field] = value
    with pytest.raises(ValidationError):
        decode_message(json.dumps(body).encode())


def test_limit_and_invalid_utf8():
    with pytest.raises(ValueError, match="64 KiB"):
        decode_message(b" " * 65537)
    with pytest.raises(ValueError):
        decode_message(b"\xff")


def test_utc_normalization_preserves_hash():
    original = command_message()
    values = original.model_dump()
    values["created_at"] = original.created_at.astimezone(timezone(timedelta(hours=-3)))
    assert encode_message(CommandEnvelope.model_validate(values)) == encode_message(original)


def test_exact_wire_limit_and_schema_payload_limits():
    body = encode_message(command_message())
    assert decode_message(body + b" " * (65536 - len(body))) == command_message()
    values = command_message().model_dump(mode="json")
    values["payload"]["description"] = "x" * 501
    with pytest.raises(ValidationError):
        decode_message(json.dumps(values).encode())


def test_result_timestamp_has_one_canonical_utc_representation():
    from fulfillflow.contracts.messages import ResultPayload, canonical_bytes

    original = result_message().payload
    fields = original.model_dump()
    fields["result"]["decided_at"] = original.result.decided_at.astimezone(
        timezone(timedelta(hours=-3))
    )
    assert canonical_bytes(ResultPayload.model_validate(fields)) == canonical_bytes(original)

"""Versioned, bounded messages for the two Tracking flows."""

import hashlib
import json
from datetime import UTC
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from fulfillflow.contracts.core import ApplyEventCommand, ContentHash, EventResult

MAX_ENVELOPE_BYTES = 65536


def canonical_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


class ResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command_sha256: ContentHash
    result: EventResult

    @field_validator("result")
    @classmethod
    def normalize_result_instant(cls, value: EventResult) -> EventResult:
        return value.model_copy(update={"decided_at": value.decided_at.astimezone(UTC)})


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    message_id: UUID
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID
    request_id: UUID
    created_at: AwareDatetime
    payload_sha256: ContentHash

    @field_validator("created_at")
    @classmethod
    def utc(cls, value: AwareDatetime) -> AwareDatetime:
        return value.astimezone(UTC)


class CommandEnvelope(Envelope):
    type: Literal["tracking.apply.v1"] = "tracking.apply.v1"
    payload: ApplyEventCommand

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.event_id != self.payload.event_id or self.causation_id != self.correlation_id:
            raise ValueError("command identity mismatch")
        if self.payload_sha256 != hashlib.sha256(canonical_bytes(self.payload)).hexdigest():
            raise ValueError("payload hash mismatch")
        return self


class ResultEnvelope(Envelope):
    type: Literal["tracking.result.v1"] = "tracking.result.v1"
    payload: ResultPayload

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.event_id != self.payload.result.event_id:
            raise ValueError("result identity mismatch")
        if self.payload_sha256 != hashlib.sha256(canonical_bytes(self.payload)).hexdigest():
            raise ValueError("payload hash mismatch")
        return self


MessageEnvelope = Annotated[CommandEnvelope | ResultEnvelope, Field(discriminator="type")]
_MESSAGE = TypeAdapter[MessageEnvelope](MessageEnvelope)


def encode_message(message: MessageEnvelope) -> bytes:
    body = canonical_bytes(message)
    if len(body) > MAX_ENVELOPE_BYTES:
        raise ValueError("envelope exceeds 64 KiB")
    return body


def decode_message(body: bytes) -> MessageEnvelope:
    if len(body) > MAX_ENVELOPE_BYTES:
        raise ValueError("envelope exceeds 64 KiB")
    return _MESSAGE.validate_json(body)

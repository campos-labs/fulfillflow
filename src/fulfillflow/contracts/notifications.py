"""Notifications HTTP projections independent of service implementations."""

from datetime import UTC
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

NotificationStatus = Literal["SIMULATED", "FAILED"]
NotificationOrigin = Literal["ASYNC", "LEGACY"]
NotificationProcessing = Literal["NOT_RECEIVED", "PENDING", "RETRY_WAIT", "BLOCKED", "DONE"]
NotificationPublication = Literal["PENDING", "LEASED", "SENT", "BLOCKED"]


class NotificationRead(BaseModel):
    """Preserved public terminal projection; causal payload and transport stay private."""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)
    id: UUID
    shipment_id: UUID
    tracking_event_id: UUID
    channel: Literal["EMAIL"]
    recipient: Annotated[str, Field(min_length=1, max_length=254)]
    template_key: Annotated[str, Field(min_length=1, max_length=80)]
    message: Annotated[str, Field(min_length=1)]
    status: NotificationStatus
    error_detail: str | None
    created_at: AwareDatetime
    simulated_at: AwareDatetime | None

    @field_validator("created_at", "simulated_at")
    @classmethod
    def utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return value.astimezone(UTC) if value is not None else None


class NotificationList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[NotificationRead]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class LegacyNotificationArchive(BaseModel):
    """Offline owner export; no synthetic transport history is introduced."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    notifications: list[NotificationRead]
    applied_event_ids: list[UUID]

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        ids = {item.id for item in self.notifications}
        events = {item.tracking_event_id for item in self.notifications}
        if len(ids) != len(self.notifications) or len(events) != len(self.notifications):
            raise ValueError("legacy identities must be unique")
        if len(set(self.applied_event_ids)) != len(self.applied_event_ids):
            raise ValueError("applied identities must be unique")
        if not set(self.applied_event_ids).issubset(events):
            raise ValueError("every applied receipt requires its original notification")
        return self


class NotificationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    simulated: int = Field(ge=0)
    failed: int = Field(ge=0)
    observed_at: AwareDatetime


class NotificationProgress(BaseModel):
    """Owner observation; an absent record does not establish whether it is required."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tracking_event_id: UUID
    processing: NotificationProcessing | None
    notification_id: UUID | None = None
    status: NotificationStatus | None = None
    simulated_at: AwareDatetime | None = None
    origin: NotificationOrigin | None
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        terminal = self.notification_id is not None and self.status is not None
        if (self.notification_id is None) != (self.status is None):
            raise ValueError("terminal identity and status must appear together")
        if self.origin == "LEGACY":
            if not terminal or self.processing is not None:
                raise ValueError("legacy observations require a terminal and no processing state")
        elif self.origin == "ASYNC":
            if self.processing not in ("PENDING", "RETRY_WAIT", "BLOCKED", "DONE"):
                raise ValueError("async observations require a technical processing state")
            if terminal != (self.processing == "DONE"):
                raise ValueError("a completed async inbox requires its terminal record")
        elif self.processing != "NOT_RECEIVED" or terminal:
            raise ValueError("unknown effects must be not received")
        if (self.status == "SIMULATED") != (self.simulated_at is not None):
            raise ValueError("only simulated records have a simulation timestamp")
        return self


class NotificationStatusView(BaseModel):
    """Core decision plus separate local/remote observations, never a global snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tracking_event_id: UUID
    required: bool
    publication: NotificationPublication | None = None
    processing: NotificationProcessing | None = None
    notification_id: UUID | None = None
    status: NotificationStatus | None = None
    simulated_at: AwareDatetime | None = None
    origin: NotificationOrigin | None = None
    core_observed_at: AwareDatetime
    notifications_observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if not self.required:
            if any(
                value is not None
                for value in (
                    self.publication,
                    self.processing,
                    self.notification_id,
                    self.status,
                    self.simulated_at,
                    self.origin,
                    self.notifications_observed_at,
                )
            ):
                raise ValueError("an effect requiring no notification has no remote observation")
            return self
        if self.notifications_observed_at is None:
            raise ValueError("required effects need a remote observation")
        if self.origin == "LEGACY":
            if self.publication is not None:
                raise ValueError("legacy effects have no publication")
        elif self.origin != "ASYNC" or self.publication is None:
            raise ValueError("async effects require a publication")
        NotificationProgress(
            tracking_event_id=self.tracking_event_id,
            processing=self.processing,
            notification_id=self.notification_id,
            status=self.status,
            simulated_at=self.simulated_at,
            origin=None if self.processing == "NOT_RECEIVED" else self.origin,
            observed_at=self.notifications_observed_at,
        )
        return self

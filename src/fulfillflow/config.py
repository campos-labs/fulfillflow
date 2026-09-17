"""Validated application configuration."""

from __future__ import annotations

from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_MINIMUM_SECRET_LENGTH = 32
_PLACEHOLDER_MARKERS = (
    "changeme",
    "example",
    "placeholder",
    "replaceme",
    "setme",
)


class DatabaseSettings(BaseSettings):
    """PostgreSQL settings shared by the application and Alembic."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
        validate_default=True,
    )

    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=0, ge=0)
    db_pool_timeout_seconds: float = Field(default=5, gt=0)
    db_statement_timeout_ms: int = Field(default=5000, gt=0)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        """Accept only SQLAlchemy's asynchronous psycopg PostgreSQL dialect."""
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from exc

        if url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use the postgresql+psycopg dialect")
        if not url.database:
            raise ValueError("DATABASE_URL must name a PostgreSQL database")
        return value

    @property
    def database_dsn(self) -> str:
        """Return the DSN only at the database integration boundary."""
        return self.database_url.get_secret_value()


class Settings(DatabaseSettings):
    """Complete FulfillFlow startup configuration."""

    app_env: Literal["local", "test", "benchmark"] = "local"
    app_name: str = Field(default="FulfillFlow", min_length=1)
    app_host: str = Field(default="0.0.0.0", min_length=1)
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_timezone: str = "America/Sao_Paulo"

    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    service_role: Literal["core", "tracking", "notifications"] = "core"
    amqp_url: SecretStr | None = None
    internal_api_secret: SecretStr
    core_base_url: AnyHttpUrl = AnyHttpUrl("http://core:8000")
    tracking_base_url: AnyHttpUrl = AnyHttpUrl("http://tracking:8000")
    notifications_base_url: AnyHttpUrl = AnyHttpUrl("http://notifications:8000")
    notifications_api_secret: SecretStr = SecretStr("")
    notifications_http_timeout_seconds: float = Field(default=2, gt=0, le=60)
    service_http_timeout_seconds: float = Field(default=10, gt=0, le=60)
    forwarding_timeout_seconds: float = Field(default=30, gt=0, le=120)
    session_secret: SecretStr = SecretStr("")
    carrier_alpha_webhook_secret: SecretStr = SecretStr("")
    carrier_beta_webhook_secret: SecretStr = SecretStr("")
    webhook_signature_tolerance_seconds: int = Field(default=300, gt=0)
    max_webhook_body_bytes: int = Field(default=65536, gt=0, le=65536)

    metrics_enabled: bool = True
    otel_enabled: bool = False
    otel_service_name: str = Field(default="fulfillflow", min_length=1)
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None
    otel_traces_sampler: str = Field(default="parentbased_traceidratio", min_length=1)
    otel_traces_sampler_arg: float = Field(default=1.0, ge=0.0, le=1.0)

    seed_random_seed: int = 20260828

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Allow conventional lowercase log levels in environment files."""
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Reject unknown IANA timezone identifiers at startup."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("APP_TIMEZONE must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_conditional_settings(self) -> Self:
        """Validate secret invariants and conditional OpenTelemetry settings."""
        alpha_secret = self.carrier_alpha_webhook_secret.get_secret_value()
        beta_secret = self.carrier_beta_webhook_secret.get_secret_value()

        if self.service_role == "tracking" and (not alpha_secret or not beta_secret):
            raise ValueError("Tracking requires both carrier webhook secrets")
        if (alpha_secret or beta_secret) and alpha_secret == beta_secret:
            raise ValueError("carrier webhook secrets must be distinct")

        if self.app_env != "test":
            secrets = {"INTERNAL_API_SECRET": self.internal_api_secret.get_secret_value()}
            if self.service_role == "core":
                secrets["SESSION_SECRET"] = self.session_secret.get_secret_value()
            elif self.service_role == "tracking":
                secrets["CARRIER_ALPHA_WEBHOOK_SECRET"] = alpha_secret
                secrets["CARRIER_BETA_WEBHOOK_SECRET"] = beta_secret
            trivial = [name for name, value in secrets.items() if _is_trivial_secret(value)]
            if trivial:
                names = ", ".join(trivial)
                raise ValueError(f"non-test secrets must be non-trivial: {names}")

        if not self.internal_api_secret.get_secret_value():
            raise ValueError("INTERNAL_API_SECRET must not be empty")

        if self.otel_enabled and self.otel_exporter_otlp_endpoint is None:
            raise ValueError("OTEL_EXPORTER_OTLP_ENDPOINT is required when OTEL_ENABLED=true")

        return self


def _is_trivial_secret(value: str) -> bool:
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    return len(value) < _MINIMUM_SECRET_LENGTH or any(
        marker in normalized for marker in _PLACEHOLDER_MARKERS
    )

"""Configuration validation tests."""

from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from fulfillflow.config import DatabaseSettings, Settings
from fulfillflow.db import Database


def test_settings_apply_documented_defaults(settings: Settings) -> None:
    assert settings.app_name == "FulfillFlow"
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 8000
    assert settings.app_timezone == "America/Sao_Paulo"
    assert settings.db_pool_size == 5
    assert settings.db_max_overflow == 0
    assert settings.db_pool_timeout_seconds == 5
    assert settings.db_statement_timeout_ms == 5000
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.metrics_enabled is True
    assert settings.otel_enabled is False
    assert settings.seed_random_seed == 20260828
    assert ZoneInfo(settings.app_timezone).key == "America/Sao_Paulo"


def test_service_secrets_are_scoped_to_the_verifying_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("SESSION_SECRET", "CARRIER_ALPHA_WEBHOOK_SECRET", "CARRIER_BETA_WEBHOOK_SECRET"):
        monkeypatch.delenv(key, raising=False)
    common = {
        "_env_file": None,
        "app_env": "local",
        "database_url": "postgresql+psycopg://role:password@localhost/test",
        "internal_api_secret": "62f7ba93b6f5402eb1c7d5d2037898d41a9df360",
    }
    core = Settings(**common, session_secret="07ed42dc5063480abbd12a51b015216b98784361")
    assert core.service_role == "core"
    assert core.carrier_alpha_webhook_secret.get_secret_value() == ""
    tracking = Settings(
        **common,
        service_role="tracking",
        carrier_alpha_webhook_secret="1a4e92d40e0c4d878e364075258457189c3b73f8",
        carrier_beta_webhook_secret="0e6fad758c2e43d2baad9fbaf31df540f5d0c36f",
    )
    assert tracking.session_secret.get_secret_value() == ""
    notifications = Settings(**common, service_role="notifications")
    assert notifications.session_secret.get_secret_value() == ""
    assert notifications.carrier_alpha_webhook_secret.get_secret_value() == ""
    assert notifications.carrier_beta_webhook_secret.get_secret_value() == ""
    with pytest.raises(ValidationError, match="Tracking requires both"):
        Settings(**common, service_role="tracking")
    with pytest.raises(ValidationError, match="non-test secrets"):
        Settings(
            **common,
            service_role="tracking",
            carrier_alpha_webhook_secret="short",
            carrier_beta_webhook_secret="other",
        )


@pytest.mark.parametrize(
    "field",
    [
        "service_http_timeout_seconds",
        "forwarding_timeout_seconds",
        "notifications_http_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, float("inf"), float("nan")])
def test_service_timeouts_are_positive_and_finite(
    settings: Settings, field: str, value: float
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(settings.model_dump() | {field: value})


def test_internal_authentication_cannot_be_disabled_with_an_empty_token(settings: Settings) -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings.model_validate(settings.model_dump() | {"internal_api_secret": ""})


def test_required_settings_cannot_be_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "DATABASE_URL",
        "SESSION_SECRET",
        "CARRIER_ALPHA_WEBHOOK_SECRET",
        "CARRIER_BETA_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///fulfillflow.db",
        "postgresql://user:password@localhost/fulfillflow",
        "postgresql+asyncpg://user:password@localhost/fulfillflow",
        "not-a-url",
    ],
)
def test_database_url_requires_async_psycopg(database_url: str) -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg|valid SQLAlchemy URL"):
        DatabaseSettings(_env_file=None, database_url=database_url)


def test_database_url_requires_a_database_name() -> None:
    with pytest.raises(ValidationError, match="must name a PostgreSQL database"):
        DatabaseSettings(
            _env_file=None,
            database_url="postgresql+psycopg://user:password@localhost",
        )


def test_non_test_secrets_must_be_non_trivial() -> None:
    with pytest.raises(
        ValidationError,
        match="non-test secrets must be non-trivial",
    ) as error:
        Settings(
            _env_file=None,
            app_env="local",
            database_url="postgresql+psycopg://user:password@localhost/fulfillflow",
            session_secret="short",
            carrier_alpha_webhook_secret="replace-me-alpha-secret-with-a-random-value",
            carrier_beta_webhook_secret="replace-me-beta-secret-with-a-random-value",
        )

    rendered_error = str(error.value)
    assert "short" not in rendered_error
    assert "replace-me-alpha" not in rendered_error


def test_carrier_secrets_must_always_be_distinct() -> None:
    with pytest.raises(ValidationError, match="carrier webhook secrets must be distinct"):
        Settings(
            _env_file=None,
            app_env="test",
            database_url="postgresql+psycopg://user:password@localhost/fulfillflow",
            session_secret="test",
            carrier_alpha_webhook_secret="same",
            carrier_beta_webhook_secret="same",
        )


@pytest.mark.parametrize("maximum", [0, 65_537])
def test_webhook_body_limit_must_fit_the_forensic_column(maximum: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="test",
            database_url="postgresql+psycopg://user:password@localhost/fulfillflow",
            session_secret="test-session",
            carrier_alpha_webhook_secret="test-alpha",
            carrier_beta_webhook_secret="test-beta",
            max_webhook_body_bytes=maximum,
        )


def test_otel_endpoint_is_required_when_enabled(settings: Settings) -> None:
    values = settings.model_dump()
    values["otel_enabled"] = True
    values["otel_exporter_otlp_endpoint"] = None

    with pytest.raises(ValidationError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        Settings(_env_file=None, **values)


def test_log_level_is_normalized(settings: Settings) -> None:
    values = settings.model_dump()
    values["log_level"] = "debug"

    configured = Settings(_env_file=None, **values)

    assert configured.log_level == "DEBUG"


def test_database_factory_uses_psycopg_async_engine(settings: Settings) -> None:
    database = Database.from_settings(settings)

    assert database.engine.url.drivername == "postgresql+psycopg"
    assert database.engine.url.database == "fulfillflow_test"

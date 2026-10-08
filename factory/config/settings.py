"""Application configuration (environment-driven, validated).

Applies to the whole process: environment, logging, database, artifact
storage, API server, and job retry policy. Provider configuration lives in
:mod:`factory.config.provider_config`; runtime secrets are never stored in
these objects beyond what is needed to construct clients, and are registered
with the redaction layer so they can never reach logs.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from factory.config.provider_config import get_cleanapis_settings
from factory.config.youtube_config import get_youtube_settings, reset_youtube_settings_cache
from factory.errors import ConfigurationError
from factory.security.redaction import register_secret


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


def sanitize_database_url(url: str) -> str:
    """Return a log-safe representation of a database URL (credentials removed)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if parts.scheme.startswith("sqlite"):
        return url
    host = parts.hostname or "<host>"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


class AppSettings(BaseSettings):
    """Process-wide application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "ai-youtube-autonomous-factory"
    environment: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"

    database_url: str = "sqlite:///./storage/factory.db"
    artifact_storage_dir: str = "./storage/artifacts"
    artifact_schemas_dir: str = "./schemas/artifacts"

    api_host: str = "0.0.0.0"  # noqa: S104 - intentional default for containerized deployment
    api_port: int = 8080

    job_retry_base_seconds: int = Field(default=5, ge=1)
    job_retry_max_seconds: int = Field(default=3600, ge=1)

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        scheme = urlsplit(value).scheme
        if scheme not in ("sqlite", "postgresql", "postgresql+psycopg", "postgres"):
            raise ConfigurationError(
                f"Unsupported DATABASE_URL scheme {scheme!r}; expected sqlite:// or postgresql://"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigurationError(f"Invalid LOG_LEVEL: {value!r}")
        return normalized

    def validate_runtime(self) -> None:
        """Validate settings that require runtime context. Called at startup."""
        # Storage locations are validated as URLs/paths by their consumers; here
        # we only ensure the artifact schema directory looks like a path.
        if not self.artifact_schemas_dir:
            raise ConfigurationError("ARTIFACT_SCHEMAS_DIR must not be empty")


@lru_cache(maxsize=1)
def get_app_settings() -> AppSettings:
    """Load and cache application settings from the environment."""
    settings = AppSettings()
    settings.validate_runtime()
    return settings


def reset_settings_caches() -> None:
    """Clear cached settings. Intended for tests."""
    get_app_settings.cache_clear()
    get_cleanapis_settings.cache_clear()
    reset_youtube_settings_cache()


def register_runtime_secrets() -> None:
    """Register all configured secrets with the redaction layer.

    Must be called once at startup (and in tests) so that any accidental
    logging of a secret is masked. Never logs the secrets themselves.
    """
    cleanapis = get_cleanapis_settings()
    if cleanapis.cleanapis_API_KEY:
        register_secret(cleanapis.cleanapis_API_KEY)
    youtube = get_youtube_settings()
    if youtube.youtube_API_KEY:
        register_secret(youtube.youtube_API_KEY)


__all__ = [
    "AppEnv",
    "AppSettings",
    "get_app_settings",
    "register_runtime_secrets",
    "reset_settings_caches",
    "sanitize_database_url",
]

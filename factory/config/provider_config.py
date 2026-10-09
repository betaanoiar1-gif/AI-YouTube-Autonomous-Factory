"""Provider configuration — CleanAPIs (the real LLM provider for this project).

Verified CleanAPIs facts (see docs/cleanapis.md for sources):

* Base URL: ``https://cleanapis.com/v1`` (OpenAI-compatible).
* Authentication: ``Authorization: Bearer cc_...`` (``x-api-key`` also accepted).
* Keys start with ``cc_``.
* Endpoints: ``GET /models``, ``GET /models/{id}``, ``POST /chat/completions``,
  ``POST /embeddings``.
* Scopes: ``inference``, ``models:read``, ``embeddings``.

The API key is a runtime secret: it is read from the environment variable
``cleanapis_API_KEY`` (matched case-insensitively, so ``CLEANAPIS_API_KEY``
also works), is never logged, and is never written to the repository.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from factory.errors import ConfigurationError
from factory.security.urls import validate_http_url

# Verified default endpoint from CleanAPIs documentation.
CLEANAPIS_DEFAULT_BASE_URL = "https://cleanapis.com/v1"


class CleanAPISSettings(BaseSettings):
    """CleanAPIs provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Runtime secret. Field name matches the documented environment variable
    # `cleanapis_API_KEY`; case-insensitive matching also accepts
    # `CLEANAPIS_API_KEY`.
    cleanapis_API_KEY: str | None = Field(default=None)

    base_url: str = Field(default=CLEANAPIS_DEFAULT_BASE_URL)
    model: str | None = Field(default=None)

    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)

    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=86400, ge=0)

    budget_max_requests_per_job: int = Field(default=1000, ge=1)
    budget_max_total_tokens_per_job: int | None = Field(default=5_000_000, ge=1)

    structured_json_mode: bool = True

    @field_validator("cleanapis_API_KEY")
    @classmethod
    def _validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if any(ch.isspace() for ch in stripped):
            raise ConfigurationError("cleanapis_API_KEY must not contain whitespace")
        return stripped

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        from factory.errors import UnsafeURLError

        try:
            validate_http_url(value)
        except UnsafeURLError as exc:
            raise ConfigurationError(f"Invalid CLEANAPIS_BASE_URL: {exc.message}") from exc
        normalized = value.strip().rstrip("/")
        if not normalized.endswith("/v1"):
            # The OpenAI-compatible root ends in /v1; warn loudly via error so
            # misconfiguration (e.g. pointing at /v1/chat/completions) fails fast.
            raise ConfigurationError(
                f"CLEANAPIS_BASE_URL must be the API root ending in '/v1', got {value!r}"
            )
        return normalized

    def require_api_key(self) -> str:
        """Return the configured API key or raise a safe ConfigurationError.

        The error message never contains the key.
        """
        if not self.cleanapis_API_KEY:
            raise ConfigurationError(
                "cleanapis_API_KEY is not set. Export it in the environment "
                "(e.g. `export cleanapis_API_KEY=cc_...`) or add it to a local "
                ".env file (never commit .env)."
            )
        return self.cleanapis_API_KEY

    @property
    def is_configured(self) -> bool:
        """True when an API key is present. Never exposes the key itself."""
        return bool(self.cleanapis_API_KEY)


@lru_cache(maxsize=1)
def get_cleanapis_settings() -> CleanAPISSettings:
    """Load and cache CleanAPIs provider settings from the environment."""
    return CleanAPISSettings()

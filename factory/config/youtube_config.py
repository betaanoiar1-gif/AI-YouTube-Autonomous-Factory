"""YouTube Data API v3 provider configuration (intelligence plane).

Separated from application settings and from CleanAPIs provider settings,
exactly like the other provider configuration. The API key is a runtime
secret: it comes from the environment only (``youtube_API_KEY``, matched
case-insensitively so ``YOUTUBE_API_KEY`` also works), is never logged,
persisted, or printed, and is registered with the redaction layer at startup.

Only the DOCUMENTED YouTube Data API v3 is used (base URL
``https://www.googleapis.com/youtube/v3``, API-key auth via the ``key`` query
parameter, ``search``/``videos``/``channels`` list endpoints). No scraping, no
undocumented endpoints, no quota circumvention.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from factory.errors import ConfigurationError, UnsafeURLError
from factory.security.urls import validate_http_url

YOUTUBE_DEFAULT_BASE_URL = "https://www.googleapis.com/youtube/v3"

#: Documented YouTube Data API v3 quota unit costs per list call.
#: (developers.google.com/youtube/v3/determine_quota_cost)
YOUTUBE_QUOTA_COSTS: dict[str, int] = {
    "search": 100,
    "videos": 1,
    "channels": 1,
}

#: Documented default daily quota per Google Cloud project.
YOUTUBE_DEFAULT_DAILY_QUOTA = 10_000


class YouTubeSettings(BaseSettings):
    """Configuration for the YouTube Data API v3 discovery provider."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    #: Runtime secret. Case-insensitive matching also accepts YOUTUBE_API_KEY.
    youtube_API_KEY: str | None = Field(default=None)

    #: Documented API base URL. HTTPS is required by the API.
    base_url: str = Field(default=YOUTUBE_DEFAULT_BASE_URL)

    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)

    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=21600, ge=0)  # 6 hours

    #: Search page size (API maximum is 50).
    search_max_results_per_page: int = Field(default=25, ge=1, le=50)
    #: Maximum videos collected per discovery run (configurable result limit).
    discovery_result_limit: int = Field(default=50, ge=1, le=500)

    #: Per-run quota budget in documented units (a search call costs 100).
    quota_budget_units_per_run: int = Field(default=1000, ge=1)
    #: Documented default daily quota per project (informational; the API
    #: does not expose remaining quota, so it is never invented).
    quota_daily_limit: int = Field(default=YOUTUBE_DEFAULT_DAILY_QUOTA, ge=1)

    @field_validator("youtube_API_KEY")
    @classmethod
    def _validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if any(ch.isspace() for ch in value):
            raise ConfigurationError("youtube_API_KEY must not contain whitespace")
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        try:
            validate_http_url(value)
        except UnsafeURLError as exc:
            raise ConfigurationError(f"Invalid YOUTUBE_BASE_URL: {exc.message}") from exc
        normalized = value.strip().rstrip("/")
        if not normalized.lower().startswith("https://"):
            # The real API requires TLS. Plain http is allowed ONLY for
            # loopback hosts, which exist solely for the offline test
            # simulation (tests/simulated) — never for a real endpoint.
            from urllib.parse import urlsplit

            host = (urlsplit(normalized).hostname or "").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise ConfigurationError(
                    "YOUTUBE_BASE_URL must use https (the YouTube Data API requires TLS); "
                    "http is allowed only for loopback hosts (offline simulation)"
                )
        return normalized

    @property
    def is_configured(self) -> bool:
        return bool(self.youtube_API_KEY)

    def require_api_key(self) -> str:
        if not self.youtube_API_KEY:
            raise ConfigurationError(
                "youtube_API_KEY is not set. Export it in the environment "
                "(e.g. `export youtube_API_KEY=...`) or add it to a local .env file."
            )
        return self.youtube_API_KEY


@lru_cache(maxsize=1)
def get_youtube_settings() -> YouTubeSettings:
    """Load YouTube provider settings from the environment (cached)."""
    return YouTubeSettings()


def reset_youtube_settings_cache() -> None:
    """Clear the cached YouTube settings (used by tests)."""
    get_youtube_settings.cache_clear()

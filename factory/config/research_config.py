"""Research-plane configuration (Phase 2).

No paid APIs and no credentials are required: source discovery uses a
documented free search API (the Wikipedia MediaWiki API by default), and
collection goes through the SSRF-protected SafeFetcher.

Security note: ``allow_loopback`` for the fetcher is deliberately NOT a
setting — it is a constructor parameter that only the offline test simulation
uses. Production fetchers never permit plain-HTTP loopback.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Documented, free, no-key search API (Wikipedia MediaWiki API).
DEFAULT_SEARCH_API_URL = "https://en.wikipedia.org/w/api.php"


class ResearchSettings(BaseSettings):
    """Configuration for the research plane."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    #: Documented free search API used for source discovery.
    search_api_url: str = Field(default=DEFAULT_SEARCH_API_URL)
    #: Path prefix for source documents on the same origin as the search API.
    page_path_prefix: str = Field(default="/wiki/", max_length=64)

    #: Source limits per research job, by depth.
    max_sources_standard: int = Field(default=10, ge=1, le=100)
    max_sources_deep: int = Field(default=25, ge=1, le=200)

    #: Safe-fetch limits.
    fetch_timeout_seconds: float = Field(default=15.0, gt=0)
    max_collection_bytes: int = Field(default=2_000_000, ge=1)
    max_redirects: int = Field(default=5, ge=0, le=10)

    #: Source cache (dedup + fingerprinting across research jobs).
    source_cache_ttl_seconds: int = Field(default=604800, ge=0)  # 7 days
    max_cached_content_bytes: int = Field(default=1_000_000, ge=1)

    #: Evidence limits.
    max_evidence_per_source: int = Field(default=25, ge=1, le=200)

    #: Retry policy for provider calls.
    max_retries: int = Field(default=2, ge=0, le=5)

    @property
    def max_sources(self) -> int:
        """The standard-depth source limit (deep is applied by the engine)."""
        return self.max_sources_standard

    def max_sources_for_depth(self, depth: str) -> int:
        return self.max_sources_deep if depth == "deep" else self.max_sources_standard


@lru_cache(maxsize=1)
def get_research_settings() -> ResearchSettings:
    """Load research settings from the environment (cached)."""
    return ResearchSettings()


def reset_research_settings_cache() -> None:
    """Clear the cached research settings (used by tests)."""
    get_research_settings.cache_clear()

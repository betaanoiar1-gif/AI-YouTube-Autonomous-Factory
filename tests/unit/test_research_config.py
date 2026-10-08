"""Research settings tests: defaults, depth limits, loopback not configurable."""

from __future__ import annotations

import pytest

from factory.config.research_config import DEFAULT_SEARCH_API_URL, ResearchSettings


class TestResearchSettings:
    def test_defaults(self):
        settings = ResearchSettings()
        assert settings.search_api_url == DEFAULT_SEARCH_API_URL
        assert settings.search_api_url == "https://en.wikipedia.org/w/api.php"
        assert settings.max_sources_standard == 10
        assert settings.max_sources_deep == 25
        assert settings.fetch_timeout_seconds == 15.0
        assert settings.max_collection_bytes == 2_000_000
        assert settings.max_redirects == 5
        assert settings.source_cache_ttl_seconds == 604800
        assert settings.max_evidence_per_source == 25

    def test_depth_limits(self):
        settings = ResearchSettings()
        assert settings.max_sources_for_depth("standard") == 10
        assert settings.max_sources_for_depth("deep") == 25
        assert settings.max_sources_for_depth("anything-else") == 10

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch):
        # No env prefix: variable names match the field names case-insensitively.
        monkeypatch.setenv("MAX_SOURCES_STANDARD", "7")
        monkeypatch.setenv("max_sources_deep", "30")
        settings = ResearchSettings()
        assert settings.max_sources_standard == 7
        assert settings.max_sources_deep == 30

    def test_no_loopback_setting(self):
        """allow_loopback is deliberately NOT a setting: it exists only as a
        SafeFetcher constructor parameter for the offline simulation. Production
        fetchers can never enable it through configuration."""
        settings = ResearchSettings()
        assert not hasattr(settings, "allow_loopback")

"""YouTube provider configuration tests: missing key, validation, defaults."""

from __future__ import annotations

import pytest

from factory.config.youtube_config import (
    YOUTUBE_DEFAULT_BASE_URL,
    YOUTUBE_QUOTA_COSTS,
    YouTubeSettings,
)
from factory.errors import ConfigurationError


class TestYouTubeSettings:
    def test_missing_key_allowed_but_require_raises_safe_error(self):
        settings = YouTubeSettings(youtube_API_KEY=None)
        assert settings.is_configured is False
        with pytest.raises(ConfigurationError) as exc_info:
            settings.require_api_key()
        assert "youtube_API_KEY" in str(exc_info.value)

    def test_key_with_whitespace_rejected(self):
        with pytest.raises(ConfigurationError):
            YouTubeSettings(youtube_API_KEY="AIza has whitespace")

    def test_empty_key_treated_as_missing(self):
        settings = YouTubeSettings(youtube_API_KEY="   ")
        assert settings.youtube_API_KEY is None
        assert settings.is_configured is False

    def test_verified_default_base_url(self):
        settings = YouTubeSettings(youtube_API_KEY="AIzaSyTEST_x")
        assert settings.base_url == YOUTUBE_DEFAULT_BASE_URL
        assert settings.base_url == "https://www.googleapis.com/youtube/v3"

    def test_base_url_must_be_https_for_real_endpoints(self):
        with pytest.raises(ConfigurationError):
            YouTubeSettings(
                youtube_API_KEY="AIzaSyTEST_x",
                base_url="http://www.googleapis.com/youtube/v3",
            )

    def test_base_url_loopback_http_allowed_for_offline_simulation(self):
        settings = YouTubeSettings(
            youtube_API_KEY="AIzaSyTEST_x", base_url="http://127.0.0.1:9000/youtube/v3"
        )
        assert settings.base_url == "http://127.0.0.1:9000/youtube/v3"

    def test_base_url_must_be_valid_url(self):
        with pytest.raises(ConfigurationError):
            YouTubeSettings(youtube_API_KEY="AIzaSyTEST_x", base_url="not a url")

    def test_env_var_name_case_insensitive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("youtube_API_KEY", "AIzaSyFROM_ENV_lower")
        assert YouTubeSettings().youtube_API_KEY == "AIzaSyFROM_ENV_lower"
        monkeypatch.setenv("youtube_API_KEY", "")
        monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSyFROM_ENV_upper")
        assert YouTubeSettings().youtube_API_KEY == "AIzaSyFROM_ENV_upper"

    def test_defaults(self):
        settings = YouTubeSettings(youtube_API_KEY="AIzaSyTEST_x")
        assert settings.timeout_seconds == 30.0
        assert settings.max_retries == 3
        assert settings.search_max_results_per_page == 25
        assert settings.discovery_result_limit == 50
        assert settings.quota_budget_units_per_run == 1000
        assert settings.quota_daily_limit == 10_000

    def test_documented_quota_costs(self):
        # Only documented YouTube Data API v3 unit costs — never invented.
        assert YOUTUBE_QUOTA_COSTS == {"search": 100, "videos": 1, "channels": 1}

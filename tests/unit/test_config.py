"""Configuration tests: missing/invalid CleanAPIs key, defaults, validation."""

from __future__ import annotations

import pytest

from factory.config.provider_config import CLEANAPIS_DEFAULT_BASE_URL, CleanAPISSettings
from factory.config.settings import AppSettings, sanitize_database_url
from factory.errors import ConfigurationError


class TestCleanAPISSettings:
    def test_missing_key_is_allowed_but_require_raises_safe_error(self):
        settings = CleanAPISSettings(cleanapis_API_KEY=None)
        assert settings.is_configured is False
        with pytest.raises(ConfigurationError) as exc_info:
            settings.require_api_key()
        # The error message must be actionable and must NOT contain any key.
        assert "cleanapis_API_KEY" in str(exc_info.value)

    def test_key_with_whitespace_rejected(self):
        with pytest.raises(ConfigurationError):
            CleanAPISSettings(cleanapis_API_KEY="cc_has whitespace in it")

    def test_empty_key_treated_as_missing(self):
        settings = CleanAPISSettings(cleanapis_API_KEY="   ")
        assert settings.cleanapis_API_KEY is None
        assert settings.is_configured is False

    def test_verified_default_base_url(self):
        settings = CleanAPISSettings(cleanapis_API_KEY="cc_test_x")
        assert settings.base_url == CLEANAPIS_DEFAULT_BASE_URL == "https://cleanapis.com/v1"

    def test_base_url_must_end_with_v1(self):
        with pytest.raises(ConfigurationError):
            CleanAPISSettings(
                cleanapis_API_KEY="cc_test_x",
                base_url="https://cleanapis.com/v1/chat/completions",
            )

    def test_base_url_must_be_http(self):
        with pytest.raises(ConfigurationError):
            CleanAPISSettings(cleanapis_API_KEY="cc_test_x", base_url="ftp://cleanapis.com/v1")

    def test_base_url_trailing_slash_normalized(self):
        settings = CleanAPISSettings(
            cleanapis_API_KEY="cc_test_x", base_url="https://cleanapis.com/v1/"
        )
        assert settings.base_url == "https://cleanapis.com/v1"

    def test_env_var_name_case_insensitive(self, monkeypatch: pytest.MonkeyPatch):
        """The documented env var is `cleanapis_API_KEY`; CLEANAPIS_API_KEY also works."""
        monkeypatch.setenv("cleanapis_API_KEY", "cc_from_env_lower")
        assert CleanAPISSettings().cleanapis_API_KEY == "cc_from_env_lower"

        monkeypatch.setenv("cleanapis_API_KEY", "")
        monkeypatch.setenv("CLEANAPIS_API_KEY", "cc_from_env_upper")
        assert CleanAPISSettings().cleanapis_API_KEY == "cc_from_env_upper"

    def test_defaults(self):
        settings = CleanAPISSettings(cleanapis_API_KEY="cc_test_x")
        assert settings.timeout_seconds == 120.0
        assert settings.max_retries == 3
        assert settings.cache_enabled is True
        assert settings.budget_max_requests_per_job == 1000


class TestAppSettings:
    def test_database_url_scheme_validation(self):
        with pytest.raises(ConfigurationError):
            AppSettings(database_url="mysql://localhost/db")

    def test_log_level_validation(self):
        with pytest.raises(ConfigurationError):
            AppSettings(log_level="SHOUTING")

    def test_sanitize_database_url_strips_credentials(self):
        assert (
            sanitize_database_url("postgresql://user:secret@db.internal:5432/factory")
            == "postgresql://db.internal:5432/factory"
        )

    def test_sanitize_database_url_keeps_sqlite(self):
        assert (
            sanitize_database_url("sqlite:///./storage/factory.db")
            == "sqlite:///./storage/factory.db"
        )

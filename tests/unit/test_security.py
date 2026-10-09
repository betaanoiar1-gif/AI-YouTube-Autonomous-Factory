"""Security tests: path traversal, URL safety, controlled subprocess, redaction."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from factory.errors import PathTraversalError, UnsafeCommandError, UnsafeURLError
from factory.security.paths import safe_filename, safe_join, validate_storage_ref
from factory.security.redaction import (
    redact_mapping,
    redact_text,
    register_secret,
    unregister_all_secrets,
)
from factory.security.subprocess import safe_run
from factory.security.urls import validate_http_url


class TestSafeJoin:
    def test_normal_join(self, tmp_path: Path):
        result = safe_join(tmp_path, "a", "b", "c.json")
        assert result == (tmp_path / "a" / "b" / "c.json").resolve()

    def test_traversal_blocked(self, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, "..", "etc", "passwd")

    def test_traversal_via_subdirectory_blocked(self, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, "a", "..", "..", "escape")

    def test_absolute_part_blocked(self, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, "/etc/passwd")

    def test_empty_part_blocked(self, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, "")

    def test_null_byte_blocked(self, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, "a\x00b")

    def test_validate_storage_ref(self, tmp_path: Path):
        assert (
            validate_storage_ref(tmp_path, "p/t/a.v1.json")
            == (tmp_path / "p" / "t" / "a.v1.json").resolve()
        )
        with pytest.raises(PathTraversalError):
            validate_storage_ref(tmp_path, "../escape.json")
        with pytest.raises(PathTraversalError):
            validate_storage_ref(tmp_path, "/absolute.json")

    def test_safe_filename(self):
        assert safe_filename("abc-123.v2.json") == "abc-123.v2.json"
        with pytest.raises(PathTraversalError):
            safe_filename("../escape")
        with pytest.raises(PathTraversalError):
            safe_filename("a/b")
        with pytest.raises(PathTraversalError):
            safe_filename(".hidden")


class TestValidateHttpUrl:
    def test_valid_urls(self):
        assert validate_http_url("https://cleanapis.com/v1") == "https://cleanapis.com/v1"
        assert validate_http_url("http://example.com/path?q=1") == "http://example.com/path?q=1"

    def test_rejects_bad_scheme(self):
        with pytest.raises(UnsafeURLError):
            validate_http_url("file:///etc/passwd")

    def test_rejects_missing_host(self):
        with pytest.raises(UnsafeURLError):
            validate_http_url("https:///path")

    def test_rejects_embedded_credentials(self):
        with pytest.raises(UnsafeURLError):
            validate_http_url("https://user:secret@example.com/")

    def test_rejects_empty(self):
        with pytest.raises(UnsafeURLError):
            validate_http_url("")


class TestSafeRun:
    def test_runs_allowlisted_command(self, tmp_path: Path):
        result = safe_run(
            [sys.executable, "-c", "print('hello')"],
            timeout_seconds=10,
            allowed_executables={sys.executable},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_rejects_non_allowlisted_executable(self, tmp_path: Path):
        with pytest.raises(UnsafeCommandError):
            safe_run(
                [sys.executable, "-c", "print('x')"],
                timeout_seconds=10,
                allowed_executables={"/bin/definitely-not-allowed"},
            )

    def test_rejects_empty_command(self):
        with pytest.raises(UnsafeCommandError):
            safe_run([], timeout_seconds=10)

    def test_nonzero_exit_raises(self):
        with pytest.raises(UnsafeCommandError):
            safe_run(
                [sys.executable, "-c", "import sys; sys.exit(3)"],
                timeout_seconds=10,
                allowed_executables={sys.executable},
            )

    def test_timeout_raises(self):
        with pytest.raises(UnsafeCommandError):
            safe_run(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=0.5,
                allowed_executables={sys.executable},
            )

    def test_cwd_must_be_inside_workspace(self, tmp_path: Path):
        with pytest.raises(UnsafeCommandError):
            safe_run(
                [sys.executable, "-c", "print('x')"],
                timeout_seconds=10,
                cwd=Path("/etc"),
                workspace_root=tmp_path,
            )

    def test_cwd_inside_workspace_ok(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        result = safe_run(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            timeout_seconds=10,
            cwd=sub,
            workspace_root=tmp_path,
        )
        assert str(sub.resolve()) in result.stdout


class TestRedaction:
    def setup_method(self):
        unregister_all_secrets()

    def teardown_method(self):
        unregister_all_secrets()

    def test_registered_secret_is_redacted(self):
        register_secret("cc_super_secret_value_123")
        assert "cc_super_secret_value_123" not in redact_text("key is cc_super_secret_value_123 ok")
        assert "[REDACTED]" in redact_text("key is cc_super_secret_value_123 ok")

    def test_cc_pattern_redacted_without_registration(self):
        assert "[REDACTED]" in redact_text("failed with cc_abcdef1234567890abcd")

    def test_bearer_token_redacted(self):
        redacted = redact_text("Authorization: Bearer cc_abcdef1234567890abcd")
        assert "cc_abcdef1234567890abcd" not in redacted
        assert "[REDACTED]" in redacted

    def test_assignment_pattern_redacted(self):
        redacted = redact_text('config = {"api_key": "cc_secretsecretsecret"}')
        assert "cc_secretsecretsecret" not in redacted

    def test_redact_mapping_deep(self):
        register_secret("cc_deep_secret_value")
        data: dict[str, Any] = {
            "outer": {"key": "cc_deep_secret_value", "list": ["cc_deep_secret_value", 1]},
            "num": 42,
        }
        redacted = redact_mapping(data)
        assert redacted["outer"]["key"] == "[REDACTED]"
        assert redacted["outer"]["list"][0] == "[REDACTED]"
        assert redacted["num"] == 42
        # Original untouched.
        assert data["outer"]["key"] == "cc_deep_secret_value"

    def test_short_values_not_registered(self):
        register_secret("short")
        assert redact_text("short") == "short"

"""Security foundation: secret redaction, safe paths, safe URLs, safe subprocesses."""

from factory.security.paths import (
    PathTraversalError,
    safe_filename,
    safe_join,
    validate_storage_ref,
)
from factory.security.redaction import redact_mapping, redact_text, register_secret
from factory.security.subprocess import UnsafeCommandError, safe_run
from factory.security.urls import UnsafeURLError, validate_http_url

__all__ = [
    "PathTraversalError",
    "UnsafeCommandError",
    "UnsafeURLError",
    "redact_mapping",
    "redact_text",
    "register_secret",
    "safe_filename",
    "safe_join",
    "safe_run",
    "validate_http_url",
    "validate_storage_ref",
]

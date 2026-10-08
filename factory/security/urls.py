"""URL safety validation.

Provider and source URLs are validated before use: only ``http``/``https``
schemes are allowed, a hostname is required, and embedded credentials
(``user:pass@host``) are rejected so secrets cannot leak into logs or error
messages via URL echo.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from factory.errors import UnsafeURLError

ALLOWED_SCHEMES = ("http", "https")


def validate_http_url(url: str, *, allowed_schemes: tuple[str, ...] = ALLOWED_SCHEMES) -> str:
    """Validate ``url`` and return it unchanged if safe.

    Raises :class:`UnsafeURLError` for empty URLs, disallowed schemes, missing
    hostnames, or URLs containing embedded credentials.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("URL must be a non-empty string")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise UnsafeURLError(f"Malformed URL: {exc}") from exc
    if parts.scheme.lower() not in allowed_schemes:
        raise UnsafeURLError(f"URL scheme must be one of {allowed_schemes}: {parts.scheme!r}")
    if not parts.hostname:
        raise UnsafeURLError("URL must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    return url.strip()

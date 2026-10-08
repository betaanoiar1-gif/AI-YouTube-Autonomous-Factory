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


def canonicalize_url(url: str) -> str:
    """Canonicalize a URL for deduplication.

    Lowercases scheme/host, strips default ports and fragments, removes
    common tracking parameters, sorts remaining query parameters, and
    normalizes the path (no trailing slash except root).
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        netloc = host
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def url_fingerprint(url: str) -> str:
    """SHA-256 fingerprint of the canonical URL (stable dedup key)."""
    import hashlib

    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def registered_domain(host: str) -> str:
    """Best-effort registered domain (last two labels) for source independence."""
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host

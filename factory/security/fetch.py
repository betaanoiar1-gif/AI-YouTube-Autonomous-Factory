"""Safe HTTP source retrieval with SSRF protection.

Used by the research plane's source collection. Security model:

* **URL validation** — http(s) only, no embedded credentials, HTTPS by
  default; plain HTTP is allowed only for loopback hosts and only when the
  fetcher is explicitly constructed with ``allow_loopback=True`` (which exists
  solely for the offline test simulation — production fetchers never set it);
* **SSRF protection** — the hostname is resolved and EVERY resolved IP is
  checked against blocked ranges (private, loopback, link-local, reserved,
  multicast, unspecified). With ``allow_loopback`` only loopback is permitted;
  private/other special ranges are always blocked;
* **Redirect validation** — redirects are followed manually and each target
  is re-validated (scheme + resolved IP) up to ``max_redirects``;
* **Timeout, size, and content-type limits** — responses are streamed and
  capped at ``max_bytes``; only allow-listed content types are accepted;
* **No shell execution, no filesystem access** — this module performs HTTP
  retrieval only;
* **Sanitized errors** — error messages contain the reason and the
  credential-free URL, never response internals or secrets.

Known limitation (documented): the IP check resolves the hostname before the
request, so a DNS rebinding TOCTOU window remains. Full pinning would require a
custom transport; the baseline check is the standard mitigation.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from factory.errors import UnsafeURLError
from factory.observability.logging import get_logger
from factory.security.urls import canonicalize_url, validate_http_url

logger = get_logger(__name__)

#: Content types accepted for collected source material.
DEFAULT_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
    }
)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_REDIRECTS = 5


@dataclass
class CollectedDocument:
    """The result of a safe source retrieval (never raises for expected
    failures — they are marked on the result)."""

    url: str
    canonical_url: str
    ok: bool
    status_code: int | None = None
    final_url: str | None = None
    content: str = ""
    content_type: str | None = None
    byte_size: int = 0
    redirects: list[str] = field(default_factory=list)
    content_fingerprint: str = ""
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None


def normalize_content(content: str) -> str:
    """Collapse whitespace for stable content fingerprinting."""
    return " ".join(content.split())


def content_fingerprint(content: str) -> str:
    """SHA-256 of the normalized content (syndication detection)."""
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


#: RFC 6598 shared address space (CGNAT) — private-use, not globally routable.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_blocked_ip(ip: str) -> bool:
    """True for private/loopback/link-local/reserved/multicast/unspecified IPs
    (including the RFC 6598 CGNAT shared range)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → treat as blocked
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or addr in _CGNAT
    )


def resolve_host_ips(host: str) -> list[str]:
    """Resolve a hostname to IP addresses (empty list when unresolvable)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            ips.append(str(sockaddr[0]))
    return list(dict.fromkeys(ips))


def check_url_safety(url: str, *, allow_loopback: bool = False) -> str:
    """Validate a URL for safe retrieval; returns the canonical URL.

    Raises :class:`UnsafeURLError` with a sanitized reason when blocked.
    """
    # Validate the ORIGINAL URL first: canonicalization drops the userinfo
    # component, so embedded credentials must be rejected before it runs.
    try:
        validate_http_url(url)
    except UnsafeURLError as exc:
        raise UnsafeURLError(f"Unsafe source URL: {exc.message}") from exc
    canonical = canonicalize_url(url)
    try:
        validate_http_url(canonical)
    except UnsafeURLError as exc:
        raise UnsafeURLError(f"Unsafe source URL: {exc.message}") from exc
    parts = urlsplit(canonical)
    host = parts.hostname or ""
    is_https = parts.scheme.lower() == "https"
    is_loopback_host = host in ("localhost",) or host.startswith("127.") or host == "::1"
    if not is_https and not (allow_loopback and is_loopback_host):
        raise UnsafeURLError(
            "Source URLs must use HTTPS (plain HTTP is allowed only for "
            "loopback hosts in the offline test simulation)"
        )
    ips = resolve_host_ips(host)
    if not ips:
        raise UnsafeURLError(f"Source host could not be resolved: {host}")
    for ip in ips:
        if is_blocked_ip(ip):
            if allow_loopback and ipaddress.ip_address(ip).is_loopback:
                continue
            raise UnsafeURLError(
                f"Source host resolves to a blocked address ({ip}): "
                "local/private-network access is not allowed"
            )
    return canonical


class SafeFetcher:
    """SSRF-protected HTTP fetcher with size/type/redirect limits."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        allowed_content_types: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES,
        allow_loopback: bool = False,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._allowed_types = allowed_content_types
        self._allow_loopback = allow_loopback
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SafeFetcher:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def fetch(self, url: str) -> CollectedDocument:
        """Fetch a source URL safely. Expected failures are marked on the
        returned document (``ok=False``, sanitized ``error``)."""
        original = url
        try:
            canonical = check_url_safety(url, allow_loopback=self._allow_loopback)
        except UnsafeURLError as exc:
            return CollectedDocument(
                url=original,
                canonical_url=canonicalize_url(original),
                ok=False,
                error=exc.message,
            )

        current_url = canonical
        redirects: list[str] = []
        for _hop in range(self._max_redirects + 1):
            try:
                response = self._client.get(current_url)
            except httpx.TimeoutException:
                return self._failure(original, canonical, redirects, "request timed out")
            except httpx.HTTPError as exc:
                return self._failure(
                    original, canonical, redirects, f"HTTP error: {type(exc).__name__}"
                )

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return self._failure(
                        original, canonical, redirects, "redirect without Location header"
                    )
                if len(redirects) >= self._max_redirects:
                    return self._failure(original, canonical, redirects, "too many redirects")
                next_url = urljoin(current_url, location)
                try:
                    current_url = check_url_safety(next_url, allow_loopback=self._allow_loopback)
                except UnsafeURLError as exc:
                    return self._failure(
                        original, canonical, redirects, f"unsafe redirect: {exc.message}"
                    )
                redirects.append(current_url)
                continue

            if response.status_code >= 400:
                return self._failure(
                    original,
                    canonical,
                    redirects,
                    f"HTTP status {response.status_code}",
                    status_code=response.status_code,
                )

            content_type = (
                (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            )
            if content_type not in self._allowed_types:
                return self._failure(
                    original,
                    canonical,
                    redirects,
                    f"disallowed content type: {content_type or 'unknown'}",
                    status_code=response.status_code,
                )

            raw = self._read_capped(response)
            if raw is None:
                return self._failure(
                    original,
                    canonical,
                    redirects,
                    f"response exceeds maximum size ({self._max_bytes} bytes)",
                    status_code=response.status_code,
                )
            charset = response.encoding or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except (LookupError, ValueError):
                text = raw.decode("utf-8", errors="replace")

            document = CollectedDocument(
                url=original,
                canonical_url=canonical,
                ok=True,
                status_code=response.status_code,
                final_url=current_url,
                content=text,
                content_type=content_type,
                byte_size=len(raw),
                redirects=redirects,
                content_fingerprint=content_fingerprint(text),
            )
            logger.info(
                "source_collected",
                extra={
                    "url": canonical,
                    "status_code": response.status_code,
                    "byte_size": len(raw),
                    "content_type": content_type,
                },
            )
            return document

        return self._failure(original, canonical, redirects, "too many redirects")

    def _read_capped(self, response: httpx.Response) -> bytes | None:
        """Read at most ``max_bytes`` (+1 to detect oversize). None if oversize."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > self._max_bytes:
                return None
        return b"".join(chunks)

    def _failure(
        self,
        original: str,
        canonical: str,
        redirects: list[str],
        reason: str,
        *,
        status_code: int | None = None,
    ) -> CollectedDocument:
        logger.info("source_collection_failed", extra={"url": canonical, "reason": reason})
        return CollectedDocument(
            url=original,
            canonical_url=canonical,
            ok=False,
            status_code=status_code,
            redirects=redirects,
            error=reason,
        )

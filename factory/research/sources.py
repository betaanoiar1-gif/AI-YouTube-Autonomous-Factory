"""Source utilities: classification, ids, and the cross-job source cache.

* Classification is JUSTIFIED, never assumed: a source is typed by explicit
  signals (search-API hints, or domain suffixes like .gov/.edu) and carries
  authority indicators explaining why. A highly-ranked unknown source is
  never treated as authoritative.
* The source cache deduplicates collection across research jobs (by canonical
  URL fingerprint) and stores content fingerprints for syndication detection.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from factory.providers.research.types import CollectedSource, SourceCandidate
from factory.security.urls import canonicalize_url, registered_domain, url_fingerprint

#: Domain-suffix → source type (justified classification signals).
_DOMAIN_TYPE_HINTS: dict[str, str] = {
    ".gov": "government",
    ".mil": "government",
    ".edu": "academic",
    ".ac.uk": "academic",
}

#: Well-known reference domains.
_REFERENCE_DOMAINS = frozenset({"wikipedia.org", "britannica.com"})

#: Authority scores by source type (documented, deterministic).
AUTHORITY_BY_TYPE: dict[str, float] = {
    "government": 0.9,
    "academic": 0.85,
    "primary": 0.8,
    "journalism": 0.7,
    "reference": 0.6,
    "secondary": 0.4,
    "unknown": 0.2,
}


def classify_source(
    url: str,
    *,
    title: str = "",
    hint: str | None = None,
) -> tuple[str, float, list[str]]:
    """Classify a source with justified, explicit signals.

    Returns ``(source_type, authority_score, authority_indicators)``. An
    explicit ``hint`` (e.g. from the search API) takes precedence; otherwise
    the domain suffix decides; otherwise the source stays ``unknown`` with a
    low authority score — high rank is never treated as authority.
    """
    indicators: list[str] = []
    source_type = "unknown"
    if hint and hint in AUTHORITY_BY_TYPE:
        source_type = hint
        indicators.append(f"search-api-type:{hint}")
    else:
        host = (urlsplit(url).hostname or "").lower()
        domain = registered_domain(host)
        if any(host == d or host.endswith("." + d) for d in _REFERENCE_DOMAINS):
            source_type = "reference"
            indicators.append(f"reference-domain:{domain}")
        else:
            for suffix, hinted_type in _DOMAIN_TYPE_HINTS.items():
                if host.endswith(suffix) or domain.endswith(suffix):
                    source_type = hinted_type
                    indicators.append(f"domain-suffix:{suffix}")
                    break
    if not indicators:
        indicators.append("no-justified-authority-signal")
    authority = AUTHORITY_BY_TYPE.get(source_type, 0.2)
    return source_type, authority, indicators


def make_source_id(url_fp: str) -> str:
    """Deterministic source id from the URL fingerprint."""
    return f"src-{url_fp[:16]}"


def make_candidate(
    url: str,
    *,
    title: str,
    publisher: str | None = None,
    published_at: datetime | None = None,
    discovery_query: str | None = None,
    relevance_score: float = 0.0,
    hint: str | None = None,
) -> SourceCandidate:
    """Build a normalized source candidate with justified classification."""
    canonical = canonicalize_url(url)
    source_type, authority, indicators = classify_source(url, title=title, hint=hint)
    return SourceCandidate(
        url=url,
        canonical_url=canonical,
        url_fingerprint=url_fingerprint(url),
        title=title,
        publisher=publisher,
        published_at=published_at,
        source_type=source_type,
        discovery_query=discovery_query,
        relevance_score=relevance_score,
        authority_score=authority,
        authority_indicators=indicators,
    )


@dataclass
class _MemoryEntry:
    source: CollectedSource
    expires_at: float


class SourceCache(Protocol):
    """Cross-job source cache (dedup + fingerprinting)."""

    def get(self, url_fingerprint: str) -> CollectedSource | None: ...

    def put(self, source: CollectedSource) -> None: ...


class InMemorySourceCache:
    """Process-local source cache (tests / no database)."""

    def __init__(self, *, ttl_seconds: int = 604800, max_content_bytes: int = 1_000_000) -> None:
        self._entries: dict[str, _MemoryEntry] = {}
        self._ttl = ttl_seconds
        self._max_content = max_content_bytes

    def get(self, url_fingerprint: str) -> CollectedSource | None:
        entry = self._entries.get(url_fingerprint)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(url_fingerprint, None)
            return None
        return entry.source

    def put(self, source: CollectedSource) -> None:
        content = source.content
        if len(content) > self._max_content:
            content = ""  # bounded: keep fingerprints, not large copies
        cached = source.model_copy(update={"content": content, "from_cache": True})
        self._entries[source.candidate.url_fingerprint] = _MemoryEntry(
            source=cached, expires_at=time.monotonic() + self._ttl
        )

    def __len__(self) -> int:
        return len(self._entries)


class SQLiteSourceCache:
    """SQLite-backed source cache (persistent across research jobs)."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        ttl_seconds: int = 604800,
        max_content_bytes: int = 1_000_000,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl_seconds
        self._max_content = max_content_bytes

    def get(self, url_fingerprint: str) -> CollectedSource | None:
        from factory.storage.models import SourceCacheEntry  # local import: avoid cycles

        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(SourceCacheEntry, url_fingerprint)
            if row is None:
                return None
            expires_at = row.expires_at
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= now:
                    session.delete(row)
                    session.commit()
                    return None
            row.hit_count += 1
            session.commit()
            metadata = row.provider_metadata or {}
            candidate = SourceCandidate.model_validate(metadata.get("candidate", {}))
            return CollectedSource(
                candidate=candidate,
                content=row.content or "",
                content_type=row.content_type,
                byte_size=row.byte_size or 0,
                content_fingerprint=row.content_fingerprint,
                collection_status=row.status,
                collection_error=row.error,
                collected_at=row.collected_at,
                from_cache=True,
            )

    def put(self, source: CollectedSource) -> None:
        from factory.storage.models import SourceCacheEntry  # local import: avoid cycles

        content = source.content
        if len(content) > self._max_content:
            content = ""  # bounded: keep fingerprints, not large copies
        expires_at = datetime.fromtimestamp(time.time() + self._ttl, tz=UTC)
        with self._session_factory() as session:
            row = session.get(SourceCacheEntry, source.candidate.url_fingerprint)
            if row is None:
                row = SourceCacheEntry(
                    url_fingerprint=source.candidate.url_fingerprint,
                    canonical_url=source.candidate.canonical_url,
                    expires_at=expires_at,
                    provider_metadata={"candidate": source.candidate.model_dump(mode="json")},
                )
                session.add(row)
            row.content_fingerprint = source.content_fingerprint
            row.content = content
            row.content_type = source.content_type
            row.byte_size = source.byte_size
            row.status = source.collection_status
            row.error = source.collection_error
            row.collected_at = source.collected_at or datetime.now(UTC)
            row.expires_at = expires_at
            session.commit()


def source_to_item_payload(source: CollectedSource) -> dict[str, Any]:
    """Map a collected source to the SourceItem artifact contract payload
    (JSON-safe: validated and serialized through the contract model)."""
    from factory.schemas.artifacts import SourceItem

    candidate = source.candidate
    return SourceItem(
        source_id=make_source_id(candidate.url_fingerprint),
        url=candidate.url,
        canonical_url=candidate.canonical_url,
        url_fingerprint=candidate.url_fingerprint,
        title=candidate.title,
        publisher=candidate.publisher,
        published_at=candidate.published_at,
        source_type=candidate.source_type,
        discovery_query=candidate.discovery_query,
        relevance_score=candidate.relevance_score,
        authority_score=candidate.authority_score,
        authority_indicators=candidate.authority_indicators,
        collection_status=source.collection_status,
        collection_error=source.collection_error,
        collected_at=source.collected_at,
        content_fingerprint=source.content_fingerprint,
        byte_size=source.byte_size if source.byte_size else None,
        content_type=source.content_type,
    ).model_dump(mode="json")


def json_dumps_safe(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)

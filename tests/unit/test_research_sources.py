"""Research source utilities unit tests: classification, ids, caches."""

from __future__ import annotations

from factory.providers.research.types import CollectedSource
from factory.research.sources import (
    AUTHORITY_BY_TYPE,
    InMemorySourceCache,
    SQLiteSourceCache,
    classify_source,
    make_candidate,
    make_source_id,
    source_to_item_payload,
)
from factory.security.urls import url_fingerprint


class TestClassification:
    def test_hint_takes_precedence(self) -> None:
        source_type, authority, indicators = classify_source(
            "https://example.com/x", hint="academic"
        )
        assert source_type == "academic"
        assert authority == AUTHORITY_BY_TYPE["academic"]
        assert "search-api-type:academic" in indicators

    def test_gov_domain_is_government(self) -> None:
        source_type, authority, indicators = classify_source("https://www.archives.gov/x")
        assert source_type == "government"
        assert authority == 0.9
        assert any("domain-suffix:.gov" in i for i in indicators)

    def test_edu_domain_is_academic(self) -> None:
        source_type, _authority, _indicators = classify_source("https://mit.edu/x")
        assert source_type == "academic"

    def test_wikipedia_is_reference(self) -> None:
        source_type, _authority, indicators = classify_source("https://en.wikipedia.org/wiki/X")
        assert source_type == "reference"
        assert any("reference-domain" in i for i in indicators)

    def test_unknown_domain_is_not_authoritative(self) -> None:
        """A highly-ranked unknown source is never treated as authoritative."""
        source_type, authority, indicators = classify_source("https://random-blog.example/x")
        assert source_type == "unknown"
        assert authority == AUTHORITY_BY_TYPE["unknown"]
        assert "no-justified-authority-signal" in indicators

    def test_make_candidate(self) -> None:
        candidate = make_candidate(
            "https://example.com/a?utm_source=x",
            title="A",
            discovery_query="q",
            relevance_score=0.5,
        )
        assert candidate.canonical_url == "https://example.com/a"
        assert candidate.url_fingerprint == url_fingerprint("https://example.com/a?utm_source=x")
        assert candidate.title == "A"

    def test_source_id_deterministic(self) -> None:
        assert make_source_id("abc123") == make_source_id("abc123")
        assert make_source_id("abc123").startswith("src-")


def _collected(url: str = "https://example.com/a", content: str = "text") -> CollectedSource:
    candidate = make_candidate(url, title="A")
    return CollectedSource(
        candidate=candidate,
        content=content,
        content_type="text/plain",
        byte_size=len(content),
        content_fingerprint="f" * 64,
        collection_status="collected",
    )


class TestInMemorySourceCache:
    def test_roundtrip_and_dedup(self) -> None:
        cache = InMemorySourceCache()
        source = _collected()
        assert cache.get(source.candidate.url_fingerprint) is None
        cache.put(source)
        hit = cache.get(source.candidate.url_fingerprint)
        assert hit is not None
        assert hit.from_cache is True
        assert hit.content == "text"

    def test_content_bounded(self) -> None:
        cache = InMemorySourceCache(max_content_bytes=4)
        source = _collected(content="x" * 100)
        cache.put(source)
        hit = cache.get(source.candidate.url_fingerprint)
        assert hit is not None
        assert hit.content == ""  # large content is not cached (fingerprint kept)
        assert hit.content_fingerprint

    def test_ttl_expiry(self) -> None:
        cache = InMemorySourceCache(ttl_seconds=0)
        source = _collected()
        cache.put(source)
        import time

        time.sleep(0.01)
        assert cache.get(source.candidate.url_fingerprint) is None


class TestSQLiteSourceCache:
    def test_roundtrip(self, session_factory) -> None:
        cache = SQLiteSourceCache(session_factory)
        source = _collected()
        assert cache.get(source.candidate.url_fingerprint) is None
        cache.put(source)
        hit = cache.get(source.candidate.url_fingerprint)
        assert hit is not None
        assert hit.from_cache is True
        assert hit.content == "text"
        assert hit.candidate.title == "A"

        from sqlalchemy import select

        from factory.storage.models import SourceCacheEntry

        with session_factory() as session:
            row = session.execute(
                select(SourceCacheEntry).where(
                    SourceCacheEntry.url_fingerprint == source.candidate.url_fingerprint
                )
            ).scalar_one()
            assert row.hit_count == 1
            assert row.content_fingerprint == "f" * 64

    def test_expired_entry_removed(self, session_factory) -> None:
        cache = SQLiteSourceCache(session_factory, ttl_seconds=0)
        source = _collected()
        cache.put(source)
        assert cache.get(source.candidate.url_fingerprint) is None


class TestSourceItemPayload:
    def test_payload_is_json_safe_and_valid(self) -> None:
        import json

        from factory.schemas.artifacts import SourceItem

        payload = source_to_item_payload(_collected())
        json.dumps(payload)  # must be JSON-serializable
        item = SourceItem.model_validate(payload)
        assert item.source_id.startswith("src-")
        assert item.collection_status == "collected"
        assert item.content_fingerprint == "f" * 64

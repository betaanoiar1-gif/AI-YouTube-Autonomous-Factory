"""LLM cache tests: key determinism, TTL, persistence, hit counting."""

from __future__ import annotations

import time

from factory.providers.cache import InMemoryLLMCache, SQLiteLLMCache, compute_cache_key
from factory.providers.llm.types import LLMRequest, LLMResponse, LLMUsage


def _request(**kwargs) -> LLMRequest:
    defaults = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.0}
    defaults.update(kwargs)
    return LLMRequest(**defaults)


def _response(content: str = "ok") -> LLMResponse:
    return LLMResponse(
        content=content,
        provider="cleanapis",
        model="test-model",
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        latency_ms=5,
    )


class TestCacheKey:
    def test_deterministic_for_same_request(self):
        key1 = compute_cache_key(_request(), provider="cleanapis", model="m")
        key2 = compute_cache_key(_request(), provider="cleanapis", model="m")
        assert key1 == key2

    def test_differs_for_different_content(self):
        key1 = compute_cache_key(_request(), provider="cleanapis", model="m")
        key2 = compute_cache_key(
            _request(messages=[{"role": "user", "content": "different"}]),
            provider="cleanapis",
            model="m",
        )
        assert key1 != key2

    def test_differs_for_different_model(self):
        key1 = compute_cache_key(_request(), provider="cleanapis", model="m1")
        key2 = compute_cache_key(_request(), provider="cleanapis", model="m2")
        assert key1 != key2

    def test_system_preamble_affects_key(self):
        key1 = compute_cache_key(_request(), provider="cleanapis", model="m")
        key2 = compute_cache_key(_request(system="be terse"), provider="cleanapis", model="m")
        assert key1 != key2


class TestInMemoryCache:
    def test_roundtrip(self):
        cache = InMemoryLLMCache()
        key = compute_cache_key(_request(), provider="cleanapis", model="m")
        assert cache.get(key) is None
        cache.set(key, _response("cached"), ttl_seconds=60)
        hit = cache.get(key)
        assert hit is not None
        assert hit.content == "cached"

    def test_ttl_expiry(self):
        cache = InMemoryLLMCache()
        key = compute_cache_key(_request(), provider="cleanapis", model="m")
        cache.set(key, _response(), ttl_seconds=0)
        time.sleep(0.01)
        assert cache.get(key) is None

    def test_lru_eviction(self):
        cache = InMemoryLLMCache(max_entries=2)
        keys = [
            compute_cache_key(
                _request(messages=[{"role": "user", "content": str(i)}]), provider="p", model="m"
            )
            for i in range(3)
        ]
        for key in keys:
            cache.set(key, _response(), ttl_seconds=60)
        assert len(cache) == 2


class TestSQLiteCache:
    def test_roundtrip_and_hit_count(self, session_factory):
        cache = SQLiteLLMCache(session_factory)
        key = compute_cache_key(_request(), provider="cleanapis", model="m")
        assert cache.get(key) is None
        cache.set(key, _response("persisted"), ttl_seconds=3600)
        hit = cache.get(key)
        assert hit is not None and hit.content == "persisted"
        hit_again = cache.get(key)
        assert hit_again is not None

        from sqlalchemy import select

        from factory.storage.models import LLMCacheEntry

        with session_factory() as session:
            row = session.execute(
                select(LLMCacheEntry).where(LLMCacheEntry.cache_key == key)
            ).scalar_one()
            assert row.hit_count == 2
            assert row.provider == "cleanapis"

    def test_expired_entry_removed(self, session_factory):
        cache = SQLiteLLMCache(session_factory)
        key = compute_cache_key(_request(), provider="cleanapis", model="m")
        cache.set(key, _response(), ttl_seconds=0)
        assert cache.get(key) is None

    def test_update_existing_entry(self, session_factory):
        cache = SQLiteLLMCache(session_factory)
        key = compute_cache_key(_request(), provider="cleanapis", model="m")
        cache.set(key, _response("v1"), ttl_seconds=3600)
        cache.set(key, _response("v2"), ttl_seconds=3600)
        updated = cache.get(key)
        assert updated is not None and updated.content == "v2"

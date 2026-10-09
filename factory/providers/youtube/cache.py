"""Discovery response caching (avoids unnecessary paid API calls).

Mirrors the LLM cache design: a cache key over the normalized request
(provider + endpoint + parameters), TTL-based expiry, and hit counting.
Backends: in-memory (tests / no database) and SQLite (``discovery_cache``).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session


def compute_discovery_cache_key(provider: str, endpoint: str, params: dict[str, Any]) -> str:
    """Stable SHA-256 cache key over provider + endpoint + normalized params."""
    canonical = json.dumps(
        {"provider": provider, "endpoint": endpoint, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DiscoveryCache(Protocol):
    """Protocol for discovery response caches."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, response: dict[str, Any], *, ttl_seconds: int) -> None: ...


@dataclass
class _MemoryEntry:
    response: dict[str, Any]
    expires_at: float


class InMemoryDiscoveryCache:
    """Process-local discovery cache (LRU-bounded)."""

    def __init__(self, *, max_entries: int = 512) -> None:
        self._entries: dict[str, _MemoryEntry] = {}
        self._max_entries = max_entries

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        # Refresh LRU position.
        self._entries[key] = self._entries.pop(key)
        return entry.response

    def set(self, key: str, response: dict[str, Any], *, ttl_seconds: int) -> None:
        if len(self._entries) >= self._max_entries and key not in self._entries:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = _MemoryEntry(
            response=response, expires_at=time.monotonic() + ttl_seconds
        )

    def __len__(self) -> int:
        return len(self._entries)


class SQLiteDiscoveryCache:
    """SQLite-backed discovery cache (persistent, shared across runs)."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: str) -> dict[str, Any] | None:
        from factory.storage.models import DiscoveryCacheEntry  # local import: avoid cycles

        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(DiscoveryCacheEntry, key)
            if row is None:
                return None
            expires_at = row.expires_at
            if expires_at is not None:
                # SQLite returns naive datetimes; normalize to UTC for comparison.
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= now:
                    session.delete(row)
                    session.commit()
                    return None
            row.hit_count += 1
            session.commit()
            cached: dict[str, Any] = json.loads(row.response_json)
            return cached

    def set(self, key: str, response: dict[str, Any], *, ttl_seconds: int) -> None:
        from factory.storage.models import DiscoveryCacheEntry  # local import: avoid cycles

        # ttl_seconds=0 expires immediately (consistent with the memory cache).
        expires_at = datetime.fromtimestamp(time.time() + ttl_seconds, tz=UTC)
        payload = json.dumps(response, default=str)
        with self._session_factory() as session:
            row = session.get(DiscoveryCacheEntry, key)
            if row is None:
                session.add(
                    DiscoveryCacheEntry(
                        cache_key=key,
                        provider="youtube",
                        endpoint="discovery",
                        request_json="{}",
                        response_json=payload,
                        expires_at=expires_at,
                    )
                )
            else:
                row.response_json = payload
                row.expires_at = expires_at
                row.hit_count = 0
            session.commit()

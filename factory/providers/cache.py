"""LLM response cache (deduplication of deterministic requests).

The same deterministic request (same provider, model, messages, and sampling
parameters, temperature 0) must not hit the provider twice. The cache key is
a SHA-256 over the normalized request; responses are stored with a TTL.

Two interchangeable backends:

* :class:`SQLiteLLMCache` — persistent, survives restarts (``llm_cache`` table).
* :class:`InMemoryLLMCache` — process-local LRU, used in tests or when no
  database is configured.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from factory.providers.llm.types import LLMRequest, LLMResponse


def compute_cache_key(request: LLMRequest, *, provider: str, model: str) -> str:
    """Compute a deterministic cache key for a normalized request."""
    messages = [
        {"role": m.role, "content": m.content, "name": m.name, "tool_call_id": m.tool_call_id}
        for m in request.effective_messages()
    ]
    normalized = {
        "provider": provider,
        "model": model,
        "messages": messages,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "top_p": request.top_p,
        "stop": request.stop,
        "seed": request.seed,
        "json_mode": request.json_mode,
    }
    blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class LLMResponseCache(Protocol):
    def get(self, key: str) -> LLMResponse | None: ...

    def set(self, key: str, response: LLMResponse, *, ttl_seconds: int) -> None: ...


class InMemoryLLMCache:
    """Process-local LRU cache with TTL. Intended for tests and small jobs."""

    def __init__(self, max_entries: int = 512) -> None:
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._max_entries = max_entries

    def get(self, key: str) -> LLMResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at <= time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return LLMResponse.model_validate_json(payload)

    def set(self, key: str, response: LLMResponse, *, ttl_seconds: int) -> None:
        self._entries[key] = (time.monotonic() + ttl_seconds, response.model_dump_json())
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class SQLiteLLMCache:
    """Persistent cache backed by the ``llm_cache`` table."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: str) -> LLMResponse | None:
        from factory.storage.models import LLMCacheEntry  # local import: avoid cycles

        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(LLMCacheEntry, key)
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
            return LLMResponse.model_validate_json(row.response_json)

    def set(self, key: str, response: LLMResponse, *, ttl_seconds: int) -> None:
        from factory.storage.models import LLMCacheEntry  # local import: avoid cycles

        # ttl_seconds=0 expires immediately (consistent with the memory cache).
        expires_at = datetime.fromtimestamp(time.time() + ttl_seconds, tz=UTC)
        with self._session_factory() as session:
            row = session.get(LLMCacheEntry, key)
            if row is None:
                row = LLMCacheEntry(
                    cache_key=key,
                    provider=response.provider,
                    model=response.model,
                    response_json=response.model_dump_json(),
                    hit_count=0,
                    expires_at=expires_at,
                )
                session.add(row)
            else:
                row.response_json = response.model_dump_json()
                row.expires_at = expires_at
            session.commit()


@dataclass
class CachedResponse:
    response: LLMResponse

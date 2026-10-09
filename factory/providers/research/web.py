"""WebSourceProvider — the production SourceProvider (Phase 2).

* **Discovery** uses a documented, free, no-key search API (the Wikipedia
  MediaWiki API by default: ``action=query&list=search``). The search API URL
  and page-path prefix are configurable, so the same production code path is
  exercised against the offline simulation.
* **Collection** goes through the SSRF-protected :class:`SafeFetcher`
  (HTTPS by default, URL + redirect validation, resolved-IP blocking, size
  and content-type limits, timeouts).
* **Deduplication** via the cross-job source cache (canonical URL
  fingerprint + content fingerprint for syndication detection).
* Every call records provider usage (latency, success/failure, metadata) —
  never credentials.

No scraping, no browser automation, no undocumented endpoints, no robots/access
control bypass. Sources that cannot be safely retrieved keep their metadata and
are marked failed — content is never invented.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from factory.config.research_config import ResearchSettings, get_research_settings
from factory.errors import (
    MalformedResponseError,
    ProviderError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitError,
)
from factory.observability.logging import get_logger
from factory.observability.usage import LoggingUsageTracker, ProviderUsageRecord, UsageTracker
from factory.providers.research.base import SourceProvider
from factory.providers.research.types import CollectedSource, SourceCandidate
from factory.research.sources import SourceCache, make_candidate
from factory.security.fetch import SafeFetcher

logger = get_logger(__name__)

PROVIDER_NAME = "web-sources"


class WebSourceProvider(SourceProvider):
    """Source discovery + safe collection behind the SourceProvider contract."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        settings: ResearchSettings | None = None,
        fetcher: SafeFetcher | None = None,
        source_cache: SourceCache | None = None,
        usage_tracker: UsageTracker | None = None,
        http_client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings or get_research_settings()
        self._fetcher = fetcher or SafeFetcher(
            timeout_seconds=self._settings.fetch_timeout_seconds,
            max_bytes=self._settings.max_collection_bytes,
            max_redirects=self._settings.max_redirects,
        )
        self._cache = source_cache
        self._usage_tracker = usage_tracker or LoggingUsageTracker()
        self._sleep = sleep
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(self._settings.fetch_timeout_seconds, connect=10.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        self._fetcher.close()

    def __enter__(self) -> WebSourceProvider:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # SourceProvider interface
    # ------------------------------------------------------------------

    def discover_sources(
        self,
        query: str,
        *,
        context: str | None = None,
        limit: int = 10,
        source_types: list[str] | None = None,
        job_id: str | None = None,
    ) -> list[SourceCandidate]:
        """Discover sources via the documented search API."""
        search = f"{query} {context}".strip() if context else query
        params: dict[str, Any] = {
            "action": "query",
            "list": "search",
            "srsearch": search,
            "srlimit": max(1, min(limit, 50)),
            "format": "json",
        }
        started = time.monotonic()
        try:
            payload = self._request_json(params, endpoint="search", job_id=job_id)
        except ProviderError as exc:
            self._record(
                endpoint="search",
                job_id=job_id,
                latency_ms=0,
                success=False,
                error_type=type(exc).__name__,
                metadata={"query": search},
            )
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        candidates = self._parse_search_results(payload, query=search, limit=limit)
        if source_types:
            candidates = [c for c in candidates if c.source_type in source_types]
        self._record(
            endpoint="search",
            job_id=job_id,
            latency_ms=latency_ms,
            success=True,
            metadata={"query": search, "results": len(candidates)},
        )
        return candidates

    def collect_source(
        self, source: SourceCandidate, *, job_id: str | None = None
    ) -> CollectedSource:
        """Safely collect a source (cache-first; never re-collects)."""
        # Cache-first dedup by canonical URL fingerprint.
        if self._cache is not None:
            cached = self._cache.get(source.url_fingerprint)
            if cached is not None:
                logger.info(
                    "source_cache_hit",
                    extra={"url": source.canonical_url, "fingerprint": source.url_fingerprint[:16]},
                )
                return cached

        started = time.monotonic()
        document = self._fetcher.fetch(source.url)
        latency_ms = int((time.monotonic() - started) * 1000)
        collected = CollectedSource(
            candidate=source,
            content=document.content if document.ok else "",
            content_type=document.content_type,
            byte_size=document.byte_size,
            content_fingerprint=document.content_fingerprint or None,
            collection_status="collected" if document.ok else "failed",
            collection_error=document.error,
            collected_at=document.collected_at,
            redirects=document.redirects,
        )
        if self._cache is not None:
            self._cache.put(collected)
        self._record(
            endpoint="collect",
            job_id=job_id,
            latency_ms=latency_ms,
            success=document.ok,
            error_type=None if document.ok else "CollectionFailed",
            metadata={
                "url_fingerprint": source.url_fingerprint,
                "byte_size": document.byte_size,
                "content_type": document.content_type,
            },
        )
        return collected

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _request_json(
        self, params: dict[str, Any], *, endpoint: str, job_id: str | None
    ) -> dict[str, Any]:
        """GET the search API with retries for 429/5xx; map errors."""
        max_retries = self._settings.max_retries
        attempt = 0
        while True:
            try:
                response = self._client.get(self._settings.search_api_url, params=params)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(
                    f"Search API request timed out: {self._settings.search_api_url}",
                    provider=PROVIDER_NAME,
                ) from exc
            except httpx.ConnectError as exc:
                raise ProviderUnavailableError(
                    f"Search API is unreachable: {self._settings.search_api_url}",
                    provider=PROVIDER_NAME,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderHTTPError(
                    f"Search API HTTP error: {exc}", provider=PROVIDER_NAME
                ) from exc

            if response.status_code < 400:
                try:
                    payload: dict[str, Any] = response.json()
                    return payload
                except (json.JSONDecodeError, ValueError) as exc:
                    raise MalformedResponseError(
                        f"Search API response is not valid JSON: {exc}",
                        provider=PROVIDER_NAME,
                    ) from exc

            error = self._map_http_error(response)
            retryable = isinstance(error, RateLimitError) or (
                isinstance(error, ProviderHTTPError)
                and error.status_code is not None
                and error.status_code >= 500
            )
            if retryable and attempt < max_retries:
                self._sleep(0.5 * (2**attempt))
                attempt += 1
                continue
            raise error

    def _map_http_error(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        kwargs: dict[str, Any] = {"provider": PROVIDER_NAME, "status_code": status}
        if status == 429:
            retry_after = response.headers.get("retry-after")
            retry_seconds = None
            if retry_after is not None:
                try:
                    retry_seconds = float(retry_after)
                except ValueError:
                    retry_seconds = None
            return RateLimitError(
                "Search API rate limit exceeded", retry_after_seconds=retry_seconds, **kwargs
            )
        return ProviderHTTPError(f"Search API error (HTTP {status})", **kwargs)

    def _parse_search_results(
        self, payload: dict[str, Any], *, query: str, limit: int
    ) -> list[SourceCandidate]:
        """Parse the documented MediaWiki search response shape."""
        try:
            results = payload["query"]["search"]
            if not isinstance(results, list):
                raise ValueError("'query.search' is not a list")
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponseError(
                f"Malformed search API response: {exc}", provider=PROVIDER_NAME
            ) from exc

        origin = self._origin()
        prefix = self._settings.page_path_prefix
        candidates: list[SourceCandidate] = []
        for index, result in enumerate(results[:limit]):
            if not isinstance(result, dict):
                continue
            title = result.get("title")
            if not isinstance(title, str) or not title:
                continue
            # Optional per-result url (tolerated extension; the documented
            # MediaWiki response does not include it — the URL is then built
            # from the search API origin).
            result_url = result.get("url")
            if isinstance(result_url, str) and result_url.startswith(("http://", "https://")):
                url = result_url
            else:
                url = f"{origin}{prefix}{quote(title.replace(' ', '_'))}"
            # Optional per-result type hint (tolerated extension; the documented
            # MediaWiki response does not include it — classification then
            # falls back to justified domain signals).
            hint = result.get("sourceType") or result.get("srctype")
            hint = hint if isinstance(hint, str) else None
            relevance = round(1.0 - (index / max(limit, 1)), 4)
            candidates.append(
                make_candidate(
                    url,
                    title=title,
                    publisher=origin.split("//")[-1],
                    discovery_query=query,
                    relevance_score=relevance,
                    hint=hint,
                )
            )
        return candidates

    def _origin(self) -> str:
        parts = urlsplit(self._settings.search_api_url)
        return f"{parts.scheme}://{parts.netloc}"

    def _record(
        self,
        *,
        endpoint: str,
        job_id: str | None,
        latency_ms: int,
        success: bool,
        error_type: str | None = None,
        metadata: dict[str, Any],
    ) -> None:
        """Record one observed provider call (never credentials)."""
        record = ProviderUsageRecord(
            provider=PROVIDER_NAME,
            model=f"search-api/{endpoint}",
            purpose="research",
            job_id=job_id,
            latency_ms=latency_ms,
            success=success,
            error_type=error_type,
            usage_available=False,
            metadata=metadata,
        )
        try:
            self._usage_tracker.record(record)
        except Exception:
            logger.exception("usage_recording_failed")

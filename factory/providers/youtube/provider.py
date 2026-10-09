"""YouTubeDiscoveryProvider — the production DiscoveryProvider implementation.

Uses the documented YouTube Data API v3 only, behind the
:class:`~factory.providers.base.DiscoveryProvider` interface. Responsibilities:

* paginate ``search`` (resumable via ``page_token``) and normalize results;
* fetch video metrics (``videos``) and channel metrics (``channels``) in
  documented batches of 50, with response caching so re-runs never spend
  quota on unnecessary calls;
* deduplicate videos across pages;
* enforce a configurable per-run quota budget (documented unit costs only —
  quota values are never invented);
* record provider usage (latency, success/failure, quota metadata) for every
  API call — the API key is never recorded.
"""

from __future__ import annotations

from typing import Any

from factory.config.youtube_config import YouTubeSettings
from factory.observability.logging import get_logger
from factory.observability.usage import LoggingUsageTracker, UsageTracker
from factory.providers.base import DiscoveryProvider
from factory.providers.discovery.types import (
    DiscoveredChannelItem,
    DiscoveredVideoItem,
    DiscoveryPage,
    DiscoveryQuery,
)
from factory.providers.youtube.cache import (
    DiscoveryCache,
    InMemoryDiscoveryCache,
    compute_discovery_cache_key,
)
from factory.providers.youtube.client import PROVIDER_NAME, YouTubeClient
from factory.providers.youtube.quota import QuotaTracker

logger = get_logger(__name__)


class YouTubeDiscoveryProvider(DiscoveryProvider):
    """DiscoveryProvider backed by the YouTube Data API v3."""

    name = PROVIDER_NAME

    def __init__(
        self,
        settings: YouTubeSettings,
        *,
        client: YouTubeClient | None = None,
        usage_tracker: UsageTracker | None = None,
        cache: DiscoveryCache | None = None,
        quota_tracker: QuotaTracker | None = None,
    ) -> None:
        self._settings = settings
        if quota_tracker is not None:
            self._quota = quota_tracker
        elif client is not None and client.quota_tracker is not None:
            # An injected client brings its own tracker — adopt it so
            # quota accounting stays single-sourced.
            self._quota = client.quota_tracker
        else:
            self._quota = QuotaTracker(budget_units=settings.quota_budget_units_per_run)
        self._usage_tracker = usage_tracker or LoggingUsageTracker()
        self._cache: DiscoveryCache | None
        if cache is not None:
            self._cache = cache
        elif settings.cache_enabled:
            self._cache = InMemoryDiscoveryCache()
        else:
            self._cache = None
        self._client = client or YouTubeClient(
            settings,
            usage_tracker=self._usage_tracker,
            quota_tracker=self._quota,
        )

    @classmethod
    def with_sqlite_cache(
        cls,
        settings: YouTubeSettings,
        session_factory: Any,
        **kwargs: Any,
    ) -> YouTubeDiscoveryProvider:
        """Build a provider whose cache and usage tracking use the database."""
        from factory.observability.usage import SQLAlchemyUsageTracker
        from factory.providers.youtube.cache import SQLiteDiscoveryCache

        kwargs.setdefault("cache", SQLiteDiscoveryCache(session_factory))
        kwargs.setdefault("usage_tracker", SQLAlchemyUsageTracker(session_factory))
        return cls(settings, **kwargs)

    # ------------------------------------------------------------------
    # DiscoveryProvider interface
    # ------------------------------------------------------------------

    def discover_videos(
        self,
        query: str | DiscoveryQuery | dict[str, Any],
        *,
        max_results: int = 50,
        job_id: str | None = None,
        page_token: str | None = None,
        max_pages: int | None = None,
    ) -> DiscoveryPage:
        """Discover videos, fetching up to ``max_pages`` pages from ``page_token``.

        Results are normalized and deduplicated by video id across pages.
        ``max_results`` caps the number of returned items.
        """
        if isinstance(query, DiscoveryQuery):
            normalized_query = query
        elif isinstance(query, dict):
            data = dict(query)
            data.setdefault("max_results_per_page", self._settings.search_max_results_per_page)
            normalized_query = DiscoveryQuery.model_validate(data)
        else:
            normalized_query = DiscoveryQuery(
                q=query, max_results_per_page=self._settings.search_max_results_per_page
            )
        if page_token:
            normalized_query = normalized_query.model_copy(update={"page_token": page_token})

        items: list[DiscoveredVideoItem] = []
        seen_ids: set[str] = set()
        next_token: str | None = None
        total_results: int | None = None
        pages_fetched = 0
        quota_units = 0

        current_token = normalized_query.page_token
        while True:
            page = self._search_page(normalized_query, current_token, job_id=job_id)
            pages_fetched += 1
            quota_units += page.quota_units_used
            total_results = page.total_results
            for item in page.items:
                if item.video_id not in seen_ids:
                    seen_ids.add(item.video_id)
                    items.append(item)
            next_token = page.next_page_token
            if len(items) >= max_results or not next_token:
                break
            if max_pages is not None and pages_fetched >= max_pages:
                break
            current_token = next_token

        return DiscoveryPage(
            items=items[:max_results],
            next_page_token=next_token,
            total_results=total_results,
            quota_units_used=quota_units,
        )

    def get_video_metrics(
        self, video_ids: list[str], *, job_id: str | None = None
    ) -> list[DiscoveredVideoItem]:
        """Fetch metrics + content details for the given videos (cached)."""
        if not video_ids:
            return []
        unique_ids = list(dict.fromkeys(video_ids))
        cached_items: list[DiscoveredVideoItem] = []
        missing_ids: list[str] = []
        if self._cache is not None:
            for video_id in unique_ids:
                cached = self._cache_get("videos", {"id": video_id})
                if cached is not None:
                    cached_items.append(DiscoveredVideoItem.model_validate(cached))
                else:
                    missing_ids.append(video_id)
        else:
            missing_ids = unique_ids
        if missing_ids:
            fetched = self._client.list_videos(missing_ids, job_id=job_id)
            if self._cache is not None:
                for item in fetched:
                    self._cache_set("videos", {"id": item.video_id}, item.model_dump(mode="json"))
            cached_items.extend(fetched)
        by_id = {item.video_id: item for item in cached_items}
        return [by_id[video_id] for video_id in unique_ids if video_id in by_id]

    def get_channel_metrics(
        self, channel_ids: str | list[str], *, job_id: str | None = None
    ) -> list[DiscoveredChannelItem]:
        """Fetch metrics for one or more channels (cached)."""
        ids = [channel_ids] if isinstance(channel_ids, str) else list(channel_ids)
        if not ids:
            return []
        unique_ids = list(dict.fromkeys(ids))
        cached_items: list[DiscoveredChannelItem] = []
        missing_ids: list[str] = []
        if self._cache is not None:
            for channel_id in unique_ids:
                cached = self._cache_get("channels", {"id": channel_id})
                if cached is not None:
                    cached_items.append(DiscoveredChannelItem.model_validate(cached))
                else:
                    missing_ids.append(channel_id)
        else:
            missing_ids = unique_ids
        if missing_ids:
            fetched = self._client.list_channels(missing_ids, job_id=job_id)
            if self._cache is not None:
                for item in fetched:
                    self._cache_set(
                        "channels", {"id": item.channel_id}, item.model_dump(mode="json")
                    )
            cached_items.extend(fetched)
        by_id = {item.channel_id: item for item in cached_items}
        return [by_id[channel_id] for channel_id in unique_ids if channel_id in by_id]

    def quota_used(self) -> int | None:
        """Documented quota units consumed through this provider instance."""
        return self._quota.used

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _search_page(
        self, query: DiscoveryQuery, page_token: str | None, *, job_id: str | None
    ) -> DiscoveryPage:
        """Fetch one search page, serving from the cache when possible."""
        params = query.api_params()
        if page_token:
            params["pageToken"] = page_token
        cache_key = None
        if self._cache is not None:
            cache_key = compute_discovery_cache_key(PROVIDER_NAME, "search", params)
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.info("discovery_cache_hit", extra={"endpoint": "search"})
                return DiscoveryPage.model_validate(cached)
        page_query = query.model_copy(update={"page_token": page_token})
        page = self._client.search(page_query, job_id=job_id)
        if cache_key is not None and self._cache is not None:
            self._cache.set(
                cache_key,
                page.model_dump(mode="json"),
                ttl_seconds=self._settings.cache_ttl_seconds,
            )
        return page

    def _cache_get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if self._cache is None:
            return None
        key = compute_discovery_cache_key(PROVIDER_NAME, endpoint, params)
        return self._cache.get(key)

    def _cache_set(self, endpoint: str, params: dict[str, Any], response: dict[str, Any]) -> None:
        if self._cache is None:
            return
        key = compute_discovery_cache_key(PROVIDER_NAME, endpoint, params)
        self._cache.set(key, response, ttl_seconds=self._settings.cache_ttl_seconds)

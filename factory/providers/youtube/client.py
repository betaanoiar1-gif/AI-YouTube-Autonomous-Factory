"""Thin, explicit client for the documented YouTube Data API v3.

Only the documented list endpoints are used:

* ``GET /search``  (part=snippet, video search, pagination)
* ``GET /videos``  (part=snippet,statistics,contentDetails, batched by id)
* ``GET /channels`` (part=snippet,statistics, batched by id)

Authentication is the documented ``key`` query parameter. The key is never
logged (it is registered with the redaction layer at startup, and the
``AIza`` key pattern is redacted as well). All errors map to typed, sanitized
:class:`~factory.errors.ProviderError` subclasses; 429/5xx are retried with
exponential backoff honoring ``Retry-After``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import httpx

from factory.config.youtube_config import YouTubeSettings
from factory.errors import (
    AuthenticationError,
    MalformedResponseError,
    ProviderConfigurationError,
    ProviderError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderValidationError,
    QuotaExceededError,
    RateLimitError,
    ResourceNotFoundError,
    ScopeError,
)
from factory.observability.logging import get_logger
from factory.observability.usage import ProviderUsageRecord, UsageTracker
from factory.providers.discovery.types import (
    DiscoveredChannelItem,
    DiscoveredVideoItem,
    DiscoveryPage,
    DiscoveryQuery,
)
from factory.providers.youtube.quota import QuotaTracker

logger = get_logger(__name__)

PROVIDER_NAME = "youtube"

#: The documented maximum number of ids per videos/channels list call.
MAX_IDS_PER_CALL = 50


def parse_iso8601_duration(value: str) -> int | None:
    """Parse an ISO 8601 duration (``PT1H2M3S``) into seconds."""
    import re

    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", value.strip()
    )
    if not match:
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return int(total)


def parse_count(value: Any) -> int | None:
    """Parse a YouTube statistics count (delivered as a string)."""
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an RFC 3339 timestamp (``2024-05-01T12:00:00Z``)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from datetime import UTC

        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class YouTubeClient:
    """Minimal, explicit client for the YouTube Data API v3."""

    def __init__(
        self,
        settings: YouTubeSettings,
        *,
        http_client: httpx.Client | None = None,
        sleep: Any = time.sleep,
        usage_tracker: UsageTracker | None = None,
        quota_tracker: QuotaTracker | None = None,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._owns_client = http_client is None
        self._usage_tracker = usage_tracker
        self._quota = quota_tracker
        api_key = settings.youtube_API_KEY
        if not api_key:
            raise ProviderConfigurationError(
                "YouTube API key is not configured (set youtube_API_KEY)",
                provider=PROVIDER_NAME,
            )
        self._api_key = api_key
        self._client = http_client or httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_seconds, connect=10.0),
        )

    @property
    def quota_tracker(self) -> QuotaTracker | None:
        """The quota tracker this client charges (None when untracked)."""
        return self._quota

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> YouTubeClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def search(self, query: DiscoveryQuery, *, job_id: str | None = None) -> DiscoveryPage:
        """``GET /search`` — one page of video search results."""
        params = query.api_params()
        payload = self._request("GET", "/search", params, endpoint="search", job_id=job_id)
        return self._parse_search_page(payload)

    def list_videos(
        self, video_ids: list[str], *, job_id: str | None = None
    ) -> list[DiscoveredVideoItem]:
        """``GET /videos`` — details for up to 50 ids per call (batched)."""
        items: list[DiscoveredVideoItem] = []
        for batch in _batched(video_ids, MAX_IDS_PER_CALL):
            params = {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
                "maxResults": len(batch),
            }
            payload = self._request("GET", "/videos", params, endpoint="videos", job_id=job_id)
            items.extend(self._parse_video_items(payload))
        return items

    def list_channels(
        self, channel_ids: list[str], *, job_id: str | None = None
    ) -> list[DiscoveredChannelItem]:
        """``GET /channels`` — details for up to 50 ids per call (batched)."""
        items: list[DiscoveredChannelItem] = []
        for batch in _batched(channel_ids, MAX_IDS_PER_CALL):
            params = {
                "part": "snippet,statistics",
                "id": ",".join(batch),
                "maxResults": len(batch),
            }
            payload = self._request("GET", "/channels", params, endpoint="channels", job_id=job_id)
            items.extend(self._parse_channel_items(payload))
        return items

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        endpoint: str,
        job_id: str | None,
    ) -> dict[str, Any]:
        """Execute a request with retries for 429/5xx; map all errors.

        The API key is sent as the documented ``key`` query parameter and is
        charged to the quota tracker before the call (conservative).
        """
        if self._quota is not None:
            self._quota.charge(endpoint, provider=PROVIDER_NAME)
        request_params = {**params, "key": self._api_key}
        max_retries = self._settings.max_retries
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = self._client.request(method, path, params=request_params)
            except httpx.TimeoutException as exc:
                self._record(
                    endpoint=endpoint,
                    job_id=job_id,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False,
                    error_type="ProviderTimeoutError",
                )
                raise ProviderTimeoutError(
                    f"YouTube API request timed out after "
                    f"{self._settings.timeout_seconds}s: {method} {path}",
                    provider=PROVIDER_NAME,
                ) from exc
            except httpx.ConnectError as exc:
                self._record(
                    endpoint=endpoint,
                    job_id=job_id,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False,
                    error_type="ProviderUnavailableError",
                )
                raise ProviderUnavailableError(
                    f"YouTube API endpoint is unreachable: {method} {path} ({exc})",
                    provider=PROVIDER_NAME,
                ) from exc
            except httpx.HTTPError as exc:
                self._record(
                    endpoint=endpoint,
                    job_id=job_id,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False,
                    error_type="ProviderHTTPError",
                )
                raise ProviderHTTPError(
                    f"YouTube API HTTP error: {exc}", provider=PROVIDER_NAME
                ) from exc

            if response.status_code < 400:
                self._record(
                    endpoint=endpoint,
                    job_id=job_id,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=True,
                )
                try:
                    payload: dict[str, Any] = response.json()
                    return payload
                except (json.JSONDecodeError, ValueError) as exc:
                    raise MalformedResponseError(
                        f"YouTube API response is not valid JSON: {exc}",
                        provider=PROVIDER_NAME,
                    ) from exc

            error = self._map_error(response, method, path)
            retryable = isinstance(error, RateLimitError) or (
                isinstance(error, ProviderHTTPError)
                and error.status_code is not None
                and error.status_code >= 500
            )
            if retryable and attempt < max_retries:
                delay = self._retry_delay(error, attempt)
                self._sleep(delay)
                attempt += 1
                continue
            self._record(
                endpoint=endpoint,
                job_id=job_id,
                latency_ms=int((time.monotonic() - started) * 1000),
                success=False,
                error_type=type(error).__name__,
            )
            raise error

    def _record(
        self,
        *,
        endpoint: str,
        job_id: str | None,
        latency_ms: int,
        success: bool,
        error_type: str | None = None,
    ) -> None:
        """Record one observed API call (never the API key)."""
        if self._usage_tracker is None:
            return
        quota_units = self._quota.cost(endpoint) if self._quota is not None else None
        record = ProviderUsageRecord(
            provider=PROVIDER_NAME,
            model=f"youtube-data-v3/{endpoint}",
            purpose="youtube_discovery",
            job_id=job_id,
            latency_ms=latency_ms,
            success=success,
            error_type=error_type,
            usage_available=False,  # the YouTube API reports no token usage
            metadata={"endpoint": endpoint, "quota_units": quota_units},
        )
        try:
            self._usage_tracker.record(record)
        except Exception:
            logger.exception("usage_recording_failed")

    def _retry_delay(self, error: ProviderError, attempt: int) -> float:
        """Exponential backoff with jitter; honors Retry-After when present."""
        if isinstance(error, RateLimitError) and error.retry_after_seconds is not None:
            retry_after: float = error.retry_after_seconds
            return max(retry_after, 0.0)
        base: float = 0.5 * (2**attempt)
        import random

        jitter: float = random.uniform(0, 0.25)  # noqa: S311 - jitter is not security-relevant
        return base + jitter

    def _map_error(self, response: httpx.Response, method: str, path: str) -> ProviderError:
        """Map a YouTube API error response to a typed, sanitized error."""
        status = response.status_code
        _code, reason, message = self._parse_error_envelope(response)
        kwargs: dict[str, Any] = {
            "provider": PROVIDER_NAME,
            "status_code": status,
            "error_code": reason,
        }
        if status == 400:
            # The documented "invalid/missing API key" responses are 400s with
            # reason keyInvalid/keyRequired — semantically authentication
            # failures, mapped accordingly.
            if reason in ("keyInvalid", "keyRequired"):
                return AuthenticationError(
                    message or "YouTube API authentication failed (invalid API key)", **kwargs
                )
            return ProviderValidationError(
                message or f"Invalid YouTube API request: {method} {path}", **kwargs
            )
        if status == 401:
            return AuthenticationError(
                message or "YouTube API authentication failed (invalid API key)", **kwargs
            )
        if status == 403:
            if reason == "quotaExceeded":
                return QuotaExceededError(message or "YouTube API daily quota exceeded", **kwargs)
            return ScopeError(
                message or "YouTube API key is not authorized for this operation", **kwargs
            )
        if status == 404:
            return ResourceNotFoundError(
                message or f"YouTube API resource not found: {method} {path}", **kwargs
            )
        if status == 429:
            retry_after = response.headers.get("retry-after")
            retry_seconds: float | None = None
            if retry_after is not None:
                try:
                    retry_seconds = float(retry_after)
                except ValueError:
                    retry_seconds = None
            return RateLimitError(
                message or "YouTube API rate limit exceeded",
                retry_after_seconds=retry_seconds,
                **kwargs,
            )
        return ProviderHTTPError(
            message or f"YouTube API error (HTTP {status}): {method} {path}", **kwargs
        )

    @staticmethod
    def _parse_error_envelope(
        response: httpx.Response,
    ) -> tuple[int | None, str | None, str | None]:
        """Extract (code, reason, message) from the documented error envelope."""
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return None, None, None
        error = payload.get("error")
        if not isinstance(error, dict):
            return None, None, None
        code = error.get("code")
        message = error.get("message")
        reason = None
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
        return (
            code if isinstance(code, int) else None,
            reason if isinstance(reason, str) else None,
            message if isinstance(message, str) else None,
        )

    # ------------------------------------------------------------------
    # Response parsing / normalization
    # ------------------------------------------------------------------

    def _parse_search_page(self, payload: dict[str, Any]) -> DiscoveryPage:
        """Parse and normalize a ``search`` list response."""
        try:
            items_raw = payload["items"]
            if not isinstance(items_raw, list):
                raise ValueError("'items' is not a list")
            items: list[DiscoveredVideoItem] = []
            for item in items_raw:
                video_id = (item.get("id") or {}).get("videoId")
                snippet = item.get("snippet") or {}
                if not isinstance(video_id, str) or not video_id:
                    continue  # non-video results are skipped (type=video filter)
                title = snippet.get("title")
                if not isinstance(title, str) or not title:
                    continue
                channel_id = snippet.get("channelId")
                if not isinstance(channel_id, str) or not channel_id:
                    continue
                description = snippet.get("description")
                items.append(
                    DiscoveredVideoItem(
                        video_id=video_id,
                        channel_id=channel_id,
                        title=title,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        published_at=parse_timestamp(snippet.get("publishedAt")),
                        channel_title=snippet.get("channelTitle")
                        if isinstance(snippet.get("channelTitle"), str)
                        else None,
                        description_chars=len(description)
                        if isinstance(description, str)
                        else None,
                    )
                )
            page_info = payload.get("pageInfo") or {}
            total = page_info.get("totalResults")
            next_token = payload.get("nextPageToken")
            return DiscoveryPage(
                items=items,
                next_page_token=next_token if isinstance(next_token, str) else None,
                total_results=total if isinstance(total, int) else None,
                quota_units_used=100,  # documented search cost
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponseError(
                f"Malformed /search response: {exc}", provider=PROVIDER_NAME
            ) from exc

    def _parse_video_items(self, payload: dict[str, Any]) -> list[DiscoveredVideoItem]:
        """Parse and normalize a ``videos`` list response."""
        try:
            items_raw = payload["items"]
            if not isinstance(items_raw, list):
                raise ValueError("'items' is not a list")
            items: list[DiscoveredVideoItem] = []
            for item in items_raw:
                video_id = item.get("id")
                if not isinstance(video_id, str) or not video_id:
                    continue
                snippet = item.get("snippet") or {}
                statistics = item.get("statistics") or {}
                content = item.get("contentDetails") or {}
                title = snippet.get("title")
                channel_id = snippet.get("channelId")
                if not isinstance(title, str) or not title:
                    continue
                if not isinstance(channel_id, str) or not channel_id:
                    continue
                duration = content.get("duration")
                tags = snippet.get("tags")
                items.append(
                    DiscoveredVideoItem(
                        video_id=video_id,
                        channel_id=channel_id,
                        title=title,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        published_at=parse_timestamp(snippet.get("publishedAt")),
                        duration_seconds=parse_iso8601_duration(duration)
                        if isinstance(duration, str)
                        else None,
                        views=parse_count(statistics.get("viewCount")) or 0,
                        likes=parse_count(statistics.get("likeCount")) or 0,
                        comments=parse_count(statistics.get("commentCount")) or 0,
                        channel_title=snippet.get("channelTitle")
                        if isinstance(snippet.get("channelTitle"), str)
                        else None,
                        description_chars=len(snippet["description"])
                        if isinstance(snippet.get("description"), str)
                        else None,
                        tags=tags if isinstance(tags, list) else [],
                        category_id=snippet.get("categoryId")
                        if isinstance(snippet.get("categoryId"), str)
                        else None,
                        definition=content.get("definition")
                        if isinstance(content.get("definition"), str)
                        else None,
                    )
                )
            return items
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponseError(
                f"Malformed /videos response: {exc}", provider=PROVIDER_NAME
            ) from exc

    def _parse_channel_items(self, payload: dict[str, Any]) -> list[DiscoveredChannelItem]:
        """Parse and normalize a ``channels`` list response."""
        try:
            items_raw = payload["items"]
            if not isinstance(items_raw, list):
                raise ValueError("'items' is not a list")
            items: list[DiscoveredChannelItem] = []
            for item in items_raw:
                channel_id = item.get("id")
                if not isinstance(channel_id, str) or not channel_id:
                    continue
                snippet = item.get("snippet") or {}
                statistics = item.get("statistics") or {}
                title = snippet.get("title")
                if not isinstance(title, str) or not title:
                    continue
                items.append(
                    DiscoveredChannelItem(
                        channel_id=channel_id,
                        title=title,
                        subscriber_count=parse_count(statistics.get("subscriberCount")),
                        view_count=parse_count(statistics.get("viewCount")),
                        video_count=parse_count(statistics.get("videoCount")),
                    )
                )
            return items
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponseError(
                f"Malformed /channels response: {exc}", provider=PROVIDER_NAME
            ) from exc


def _batched(items: list[str], size: int) -> list[list[str]]:
    """Split a list into batches of at most ``size`` (API id-list limit)."""
    return [items[i : i + size] for i in range(0, len(items), size)]

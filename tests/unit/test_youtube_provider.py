"""YouTubeDiscoveryProvider unit tests (mocked HTTP transport).

Covers the DiscoveryProvider interface implementation: pagination and
deduplication, quota tracking and budget enforcement, caching (search pages
and video/channel details), batching, normalization, and the interface
contract (business logic depends on the interface, not the vendor).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from factory.config.youtube_config import YouTubeSettings
from factory.errors import QuotaExceededError
from factory.providers.base import DiscoveryProvider
from factory.providers.youtube.cache import InMemoryDiscoveryCache
from factory.providers.youtube.provider import YouTubeDiscoveryProvider
from tests.conftest import ListUsageTracker

FAKE_KEY = "AIzaSyTEST_FAKE_KEY_0000000000000001"  # fake value for tests only


def make_settings(**overrides: Any) -> YouTubeSettings:
    base: dict[str, Any] = {
        "youtube_API_KEY": FAKE_KEY,
        "base_url": "https://www.googleapis.com/youtube/v3",
        "max_retries": 1,
        "search_max_results_per_page": 2,
    }
    base.update(overrides)
    return YouTubeSettings(**base)


def search_response(video_ids: list[str], next_token: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "youtube#searchListResponse",
        "pageInfo": {"totalResults": 100, "resultsPerPage": len(video_ids)},
        "items": [
            {
                "kind": "youtube#searchResult",
                "id": {"kind": "youtube#video", "videoId": vid},
                "snippet": {
                    "publishedAt": "2024-05-01T12:00:00Z",
                    "channelId": "UC1",
                    "title": f"Video {vid}",
                    "description": "d",
                    "channelTitle": "Chan",
                },
            }
            for vid in video_ids
        ],
    }
    if next_token:
        payload["nextPageToken"] = next_token
    return payload


def video_response(vid: str) -> dict[str, Any]:
    return {
        "kind": "youtube#video",
        "id": vid,
        "snippet": {
            "publishedAt": "2024-05-01T12:00:00Z",
            "channelId": "UC1",
            "title": f"Video {vid}",
            "description": "d",
            "channelTitle": "Chan",
            "tags": [],
            "categoryId": "22",
        },
        "statistics": {"viewCount": "1000", "likeCount": "10", "commentCount": "1"},
        "contentDetails": {"duration": "PT2M0S", "dimension": "2d", "definition": "hd"},
    }


class ScriptedTransport:
    """A mock transport with a per-endpoint script of responses."""

    def __init__(self) -> None:
        self.search_pages: list[dict[str, Any]] = []
        self.video_ids_seen: list[list[str]] = []
        self.channel_ids_seen: list[list[str]] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        if request.url.path.endswith("/search"):
            page_token = params.get("pageToken")
            index = int(page_token.split("-")[1]) if page_token else 0
            if index < len(self.search_pages):
                return httpx.Response(200, json=self.search_pages[index])
            return httpx.Response(200, json=search_response([]))
        if request.url.path.endswith("/videos"):
            ids = params["id"].split(",")
            self.video_ids_seen.append(ids)
            return httpx.Response(200, json={"items": [video_response(v) for v in ids]})
        if request.url.path.endswith("/channels"):
            ids = params["id"].split(",")
            self.channel_ids_seen.append(ids)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "youtube#channel",
                            "id": cid,
                            "snippet": {
                                "title": f"Channel {cid}",
                                "description": "d",
                                "publishedAt": "2020-01-01T00:00:00Z",
                            },
                            "statistics": {
                                "viewCount": "5000",
                                "subscriberCount": "100",
                                "videoCount": "5",
                            },
                        }
                        for cid in ids
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"code": 404, "message": "no", "errors": []}})


def make_provider(
    transport: ScriptedTransport,
    settings: YouTubeSettings | None = None,
    usage_tracker: ListUsageTracker | None = None,
    cache: InMemoryDiscoveryCache | None = None,
    budget_units: int | None = 1000,
) -> YouTubeDiscoveryProvider:
    from factory.providers.youtube.client import YouTubeClient
    from factory.providers.youtube.quota import QuotaTracker

    settings = settings or make_settings()
    http_client = httpx.Client(
        transport=httpx.MockTransport(transport.handler), base_url=settings.base_url
    )
    client = YouTubeClient(
        settings,
        http_client=http_client,
        sleep=lambda _s: None,
        usage_tracker=usage_tracker,
        quota_tracker=QuotaTracker(budget_units=budget_units),
    )
    return YouTubeDiscoveryProvider(
        settings,
        client=client,
        usage_tracker=usage_tracker,
        cache=cache if cache is not None else InMemoryDiscoveryCache(),
    )


class TestInterface:
    def test_implements_discovery_provider(self) -> None:
        transport = ScriptedTransport()
        provider = make_provider(transport)
        assert isinstance(provider, DiscoveryProvider)
        assert provider.name == "youtube"

    def test_quota_used(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [
            search_response(["a"], next_token="page-1"),
            search_response(["b"]),
        ]
        provider = make_provider(transport)
        page = provider.discover_videos("q", max_results=10)
        assert len(page.items) == 2
        assert provider.quota_used() == 200  # 2 search calls x 100


class TestPaginationAndDedup:
    def test_paginates_until_limit(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [
            search_response(["a", "b"], next_token="page-1"),
            search_response(["c", "d"], next_token="page-2"),
            search_response(["e", "f"]),
        ]
        provider = make_provider(transport)
        page = provider.discover_videos("q", max_results=5)
        assert [i.video_id for i in page.items] == ["a", "b", "c", "d", "e"]
        assert len(transport.requests) == 3

    def test_deduplicates_across_pages(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [
            search_response(["a", "b"], next_token="page-1"),
            search_response(["b", "c"], next_token="page-2"),
            search_response(["d"]),
        ]
        provider = make_provider(transport)
        page = provider.discover_videos("q", max_results=10)
        assert [i.video_id for i in page.items] == ["a", "b", "c", "d"]

    def test_resumes_from_page_token(self) -> None:
        transport = ScriptedTransport()
        # Index 0 = page 1 (no token), index 1 = the resumed page.
        transport.search_pages = [search_response(["a", "b"]), search_response(["c", "d"])]
        provider = make_provider(transport)
        page = provider.discover_videos("q", max_results=10, page_token="page-1")
        assert [i.video_id for i in page.items] == ["c", "d"]
        # The first request carried the resume token:
        assert transport.requests[0].url.params["pageToken"] == "page-1"

    def test_max_pages_bounds_calls(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [
            search_response(["a"], next_token="page-1"),
            search_response(["b"], next_token="page-2"),
            search_response(["c"], next_token="page-3"),
        ]
        provider = make_provider(transport)
        page = provider.discover_videos("q", max_results=10, max_pages=2)
        assert len(page.items) == 2
        assert len(transport.requests) == 2


class TestMetricsAndChannels:
    def test_get_video_metrics_merges_details(self) -> None:
        transport = ScriptedTransport()
        provider = make_provider(transport)
        items = provider.get_video_metrics(["a", "b", "c"])
        assert [i.video_id for i in items] == ["a", "b", "c"]
        assert all(i.views == 1000 for i in items)
        assert all(i.duration_seconds == 120 for i in items)
        assert transport.video_ids_seen == [["a", "b", "c"]]

    def test_get_channel_metrics_single_and_list(self) -> None:
        transport = ScriptedTransport()
        provider = make_provider(transport)
        single = provider.get_channel_metrics("UC1")
        assert len(single) == 1 and single[0].subscriber_count == 100
        multi = provider.get_channel_metrics(["UC1", "UC2"])
        assert len(multi) == 2

    def test_empty_input_makes_no_calls(self) -> None:
        transport = ScriptedTransport()
        provider = make_provider(transport)
        assert provider.get_video_metrics([]) == []
        assert provider.get_channel_metrics([]) == []
        assert transport.requests == []


class TestCaching:
    def test_search_pages_cached(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [search_response(["a"])]
        provider = make_provider(transport)
        provider.discover_videos("q", max_results=1)
        provider.discover_videos("q", max_results=1)
        assert len(transport.requests) == 1  # second run served from cache

    def test_video_details_cached_per_id(self) -> None:
        transport = ScriptedTransport()
        provider = make_provider(transport)
        provider.get_video_metrics(["a"])
        provider.get_video_metrics(["a"])
        assert len(transport.video_ids_seen) == 1

    def test_channel_details_cached_per_id(self) -> None:
        transport = ScriptedTransport()
        provider = make_provider(transport)
        provider.get_channel_metrics(["UC1"])
        provider.get_channel_metrics(["UC1"])
        assert len(transport.channel_ids_seen) == 1

    def test_cache_disabled_makes_real_calls(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [search_response(["a"])]
        provider = make_provider(transport, cache=None)
        provider._cache = None
        provider.discover_videos("q", max_results=1)
        provider.discover_videos("q", max_results=1)
        assert len(transport.requests) == 2


class TestQuotaBudget:
    def test_budget_enforced(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [
            search_response(["a"], next_token="page-1"),
            search_response(["b"], next_token="page-2"),
            search_response(["c"]),
        ]
        provider = make_provider(transport, budget_units=150)
        with pytest.raises(QuotaExceededError):
            provider.discover_videos("q", max_results=10)
        # Only one search call was made (the second would exceed the budget):
        search_calls = [r for r in transport.requests if r.url.path.endswith("/search")]
        assert len(search_calls) == 1

    def test_quota_counts_all_endpoints(self) -> None:
        transport = ScriptedTransport()
        transport.search_pages = [search_response(["a"])]
        provider = make_provider(transport, budget_units=1000)
        provider.discover_videos("q", max_results=1)
        provider.get_video_metrics(["a"])
        provider.get_channel_metrics(["UC1"])
        assert provider.quota_used() == 100 + 1 + 1

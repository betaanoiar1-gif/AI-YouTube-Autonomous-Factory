"""YouTube Data API v3 client tests — HTTP layer fully mocked (no network).

Covers: authentication (missing/invalid key), request shape (key as the
documented query parameter), normalization (ISO 8601 durations, string
counts, RFC 3339 timestamps), pagination parameters, batching (50 ids per
call), error mapping (400 keyInvalid/keyRequired → AuthenticationError,
400 other → validation, 401, 403 quotaExceeded → QuotaExceededError, 403 other
→ ScopeError, 404, 429 with Retry-After, 5xx), retries, timeout, malformed
responses, and per-call usage recording with quota metadata.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from factory.config.youtube_config import YouTubeSettings
from factory.errors import (
    AuthenticationError,
    ConfigurationError,
    MalformedResponseError,
    ProviderConfigurationError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderValidationError,
    QuotaExceededError,
    RateLimitError,
    ResourceNotFoundError,
    ScopeError,
)
from factory.providers.discovery.types import DiscoveryQuery
from factory.providers.youtube.client import YouTubeClient, parse_iso8601_duration, parse_timestamp
from factory.providers.youtube.quota import QuotaTracker
from tests.conftest import ListUsageTracker

FAKE_KEY = "AIzaSyTEST_FAKE_KEY_0000000000000001"  # fake value for tests only


def make_settings(**overrides: Any) -> YouTubeSettings:
    base: dict[str, Any] = {
        "youtube_API_KEY": FAKE_KEY,
        "base_url": "https://www.googleapis.com/youtube/v3",
        "max_retries": 2,
    }
    base.update(overrides)
    return YouTubeSettings(**base)


def make_client(
    handler,
    settings: YouTubeSettings | None = None,
    usage_tracker: ListUsageTracker | None = None,
    quota_tracker: QuotaTracker | None = None,
) -> YouTubeClient:
    settings = settings or make_settings()
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url=settings.base_url)
    return YouTubeClient(
        settings,
        http_client=http_client,
        sleep=lambda _s: None,
        usage_tracker=usage_tracker,
        quota_tracker=quota_tracker,
    )


def video_payload(video_id: str, *, views: int = 1000) -> dict[str, Any]:
    return {
        "kind": "youtube#video",
        "id": video_id,
        "snippet": {
            "publishedAt": "2024-05-01T12:00:00Z",
            "channelId": "UC123",
            "title": "A video",
            "description": "desc",
            "channelTitle": "Chan",
            "tags": ["a"],
            "categoryId": "22",
        },
        "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"},
        "contentDetails": {"duration": "PT1M30S", "dimension": "2d", "definition": "hd"},
    }


class TestDurationAndTimestampParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("PT1M30S", 90),
            ("PT1H2M3S", 3723),
            ("PT45S", 45),
            ("PT2H", 7200),
            ("P1DT1H", 90000),
            ("PT0S", 0),
        ],
    )
    def test_iso8601_duration(self, value: str, expected: int) -> None:
        assert parse_iso8601_duration(value) == expected

    def test_invalid_duration(self) -> None:
        assert parse_iso8601_duration("not-a-duration") is None

    def test_timestamp(self) -> None:
        parsed = parse_timestamp("2024-05-01T12:00:00Z")
        assert parsed is not None
        assert parsed.year == 2024 and parsed.month == 5
        assert parse_timestamp(None) is None
        assert parse_timestamp("garbage") is None


class TestConfiguration:
    def test_missing_key(self) -> None:
        settings = YouTubeSettings(youtube_API_KEY=None)
        with pytest.raises(ProviderConfigurationError):
            YouTubeClient(settings)

    def test_base_url_must_be_https_for_real_endpoints(self) -> None:
        with pytest.raises(ConfigurationError):
            make_settings(base_url="http://www.googleapis.com/youtube/v3")

    def test_base_url_loopback_http_allowed_for_simulation(self) -> None:
        settings = make_settings(base_url="http://127.0.0.1:8080/youtube/v3")
        assert settings.base_url == "http://127.0.0.1:8080/youtube/v3"

    def test_key_with_whitespace_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            make_settings(youtube_API_KEY="AIza has whitespace")

    def test_defaults(self) -> None:
        settings = make_settings()
        assert settings.base_url == "https://www.googleapis.com/youtube/v3"
        assert settings.search_max_results_per_page == 25
        assert settings.discovery_result_limit == 50
        assert settings.quota_budget_units_per_run == 1000


class TestSearch:
    def test_search_normalizes_and_sends_key_param(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "kind": "youtube#searchListResponse",
                    "pageInfo": {"totalResults": 1, "resultsPerPage": 1},
                    "items": [
                        {
                            "kind": "youtube#searchResult",
                            "id": {"kind": "youtube#video", "videoId": "vid1"},
                            "snippet": {
                                "publishedAt": "2024-05-01T12:00:00Z",
                                "channelId": "UC123",
                                "title": "A video",
                                "description": "d" * 100,
                                "channelTitle": "Chan",
                            },
                        }
                    ],
                },
            )

        client = make_client(handler)
        page = client.search(DiscoveryQuery(q="history", max_results_per_page=10))
        assert len(page.items) == 1
        item = page.items[0]
        assert item.video_id == "vid1"
        assert item.title == "A video"
        assert item.description_chars == 100
        assert item.published_at is not None
        assert page.total_results == 1
        assert page.quota_units_used == 100
        # The key is sent as the documented `key` query parameter:
        assert seen[0].url.params["key"] == FAKE_KEY
        assert seen[0].url.params["q"] == "history"
        assert seen[0].url.params["type"] == "video"
        assert seen[0].url.params["maxResults"] == "10"

    def test_search_pagination_params(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"items": [], "pageInfo": {}})

        client = make_client(handler)
        client.search(
            DiscoveryQuery(
                q="x",
                page_token="page-2",
                relevance_language="en",
                region_code="US",
                order="viewCount",
                video_duration="long",
            )
        )
        params = seen[0].url.params
        assert params["pageToken"] == "page-2"
        assert params["relevanceLanguage"] == "en"
        assert params["regionCode"] == "US"
        assert params["order"] == "viewCount"
        assert params["videoDuration"] == "long"

    def test_search_skips_non_video_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"kind": "youtube#channel", "channelId": "UC1"}, "snippet": {}},
                        {"id": {"kind": "youtube#video"}, "snippet": {"title": "no id"}},
                        {
                            "id": {"kind": "youtube#video", "videoId": "vid1"},
                            "snippet": {"channelId": "UC123", "title": "ok"},
                        },
                    ]
                },
            )

        client = make_client(handler)
        page = client.search(DiscoveryQuery(q="x"))
        assert [i.video_id for i in page.items] == ["vid1"]

    def test_search_malformed_missing_items(self) -> None:
        client = make_client(lambda request: httpx.Response(200, json={"pageInfo": {}}))
        with pytest.raises(MalformedResponseError):
            client.search(DiscoveryQuery(q="x"))

    def test_search_invalid_json(self) -> None:
        client = make_client(lambda request: httpx.Response(200, text="not json"))
        with pytest.raises(MalformedResponseError):
            client.search(DiscoveryQuery(q="x"))


class TestVideosAndChannels:
    def test_list_videos_normalizes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["id"] == "vid1,vid2"
            return httpx.Response(
                200,
                json={"items": [video_payload("vid1", views=5000), video_payload("vid2", views=7)]},
            )

        client = make_client(handler)
        items = client.list_videos(["vid1", "vid2"])
        assert [i.video_id for i in items] == ["vid1", "vid2"]
        assert items[0].views == 5000
        assert items[0].duration_seconds == 90
        assert items[0].likes == 10 and items[0].comments == 2
        assert items[0].tags == ["a"]
        assert items[0].category_id == "22"
        assert items[0].definition == "hd"

    def test_list_videos_batches_by_50(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            ids = request.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [video_payload(v) for v in ids]})

        client = make_client(handler)
        items = client.list_videos([f"v{i}" for i in range(120)])
        assert len(items) == 120
        assert len(calls) == 3  # 50 + 50 + 20
        assert len(calls[0].url.params["id"].split(",")) == 50

    def test_list_videos_unknown_ids_absent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [video_payload("vid1")]})

        client = make_client(handler)
        items = client.list_videos(["vid1", "missing"])
        assert [i.video_id for i in items] == ["vid1"]

    def test_list_channels_normalizes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "youtube#channel",
                            "id": "UC1",
                            "snippet": {
                                "title": "Chan",
                                "description": "d",
                                "publishedAt": "2020-01-01T00:00:00Z",
                            },
                            "statistics": {
                                "viewCount": "1000",
                                "subscriberCount": "500",
                                "videoCount": "20",
                            },
                        }
                    ]
                },
            )

        client = make_client(handler)
        channels = client.list_channels(["UC1"])
        assert channels[0].subscriber_count == 500
        assert channels[0].view_count == 1000
        assert channels[0].video_count == 20


class TestErrorMapping:
    def _client_for(
        self, status: int, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                json=body
                or {
                    "error": {
                        "code": status,
                        "message": "err",
                        "errors": [{"reason": "someReason"}],
                    }
                },
                headers=headers,
            )

        return make_client(handler)

    def test_400_key_invalid_is_authentication_error(self) -> None:
        client = self._client_for(
            400,
            {
                "error": {
                    "code": 400,
                    "message": "API key not valid.",
                    "errors": [{"reason": "keyInvalid"}],
                }
            },
        )
        with pytest.raises(AuthenticationError) as exc_info:
            client.search(DiscoveryQuery(q="x"))
        assert exc_info.value.error_code == "keyInvalid"

    def test_400_key_required_is_authentication_error(self) -> None:
        client = self._client_for(
            400,
            {
                "error": {
                    "code": 400,
                    "message": "API key required.",
                    "errors": [{"reason": "keyRequired"}],
                }
            },
        )
        with pytest.raises(AuthenticationError):
            client.search(DiscoveryQuery(q="x"))

    def test_400_other_is_validation_error(self) -> None:
        client = self._client_for(
            400,
            {"error": {"code": 400, "message": "Bad part.", "errors": [{"reason": "invalidPart"}]}},
        )
        with pytest.raises(ProviderValidationError) as exc_info:
            client.search(DiscoveryQuery(q="x"))
        assert exc_info.value.error_code == "invalidPart"

    def test_401_is_authentication_error(self) -> None:
        client = self._client_for(
            401,
            {
                "error": {
                    "code": 401,
                    "message": "Unauthorized.",
                    "errors": [{"reason": "authError"}],
                }
            },
        )
        with pytest.raises(AuthenticationError):
            client.search(DiscoveryQuery(q="x"))

    def test_403_quota_is_quota_exceeded(self) -> None:
        client = self._client_for(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "Quota exceeded.",
                    "errors": [{"reason": "quotaExceeded"}],
                }
            },
        )
        with pytest.raises(QuotaExceededError):
            client.search(DiscoveryQuery(q="x"))

    def test_403_other_is_scope_error(self) -> None:
        client = self._client_for(
            403,
            {"error": {"code": 403, "message": "Forbidden.", "errors": [{"reason": "forbidden"}]}},
        )
        with pytest.raises(ScopeError):
            client.search(DiscoveryQuery(q="x"))

    def test_404_is_resource_not_found(self) -> None:
        client = self._client_for(
            404,
            {"error": {"code": 404, "message": "Not found.", "errors": [{"reason": "notFound"}]}},
        )
        with pytest.raises(ResourceNotFoundError):
            client.search(DiscoveryQuery(q="x"))

    def test_429_is_rate_limit_with_retry_after(self) -> None:
        client = self._client_for(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "Rate limited.",
                    "errors": [{"reason": "rateLimitExceeded"}],
                }
            },
            headers={"Retry-After": "3"},
        )
        with pytest.raises(RateLimitError) as exc_info:
            client.search(DiscoveryQuery(q="x"))
        assert exc_info.value.retry_after_seconds == 3.0

    def test_500_is_http_error_and_retried(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                500,
                json={
                    "error": {
                        "code": 500,
                        "message": "backend",
                        "errors": [{"reason": "backendError"}],
                    }
                },
            )

        client = make_client(handler)
        with pytest.raises(ProviderHTTPError) as exc_info:
            client.search(DiscoveryQuery(q="x"))
        assert exc_info.value.status_code == 500
        assert len(calls) == 3  # 1 initial + 2 retries (max_retries=2)

    def test_429_retried_then_succeeds(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    429,
                    json={
                        "error": {
                            "code": 429,
                            "message": "rl",
                            "errors": [{"reason": "rateLimitExceeded"}],
                        }
                    },
                    headers={"Retry-After": "0"},
                )
            return httpx.Response(200, json={"items": [], "pageInfo": {}})

        client = make_client(handler)
        page = client.search(DiscoveryQuery(q="x"))
        assert page.items == []
        assert len(calls) == 2

    def test_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = make_client(handler, settings=make_settings(timeout_seconds=1.0))
        with pytest.raises(ProviderTimeoutError):
            client.search(DiscoveryQuery(q="x"))


class TestUsageAndQuotaRecording:
    def test_usage_recorded_per_call_with_quota_metadata(self) -> None:
        tracker = ListUsageTracker()
        quota = QuotaTracker(budget_units=1000)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [], "pageInfo": {}})

        client = make_client(handler, usage_tracker=tracker, quota_tracker=quota)
        client.search(DiscoveryQuery(q="x"), job_id="job-1")
        assert len(tracker.records) == 1
        record = tracker.records[0]
        assert record.provider == "youtube"
        assert record.model == "youtube-data-v3/search"
        assert record.purpose == "youtube_discovery"
        assert record.job_id == "job-1"
        assert record.success is True
        assert record.usage_available is False  # no tokens for this API
        assert record.metadata == {"endpoint": "search", "quota_units": 100}
        assert quota.used == 100
        # The API key is never recorded:
        assert FAKE_KEY not in json.dumps(record.to_dict())

    def test_failure_usage_recorded(self) -> None:
        tracker = ListUsageTracker()
        quota = QuotaTracker(budget_units=1000)
        client = make_client(
            lambda request: httpx.Response(
                403,
                json={
                    "error": {"code": 403, "message": "q", "errors": [{"reason": "quotaExceeded"}]}
                },
            ),
            usage_tracker=tracker,
            quota_tracker=quota,
        )
        with pytest.raises(QuotaExceededError):
            client.search(DiscoveryQuery(q="x"))
        record = tracker.records[-1]
        assert record.success is False
        assert record.error_type == "QuotaExceededError"

    def test_quota_budget_blocks_before_call(self) -> None:
        tracker = ListUsageTracker()
        quota = QuotaTracker(budget_units=150)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"items": [], "pageInfo": {}})

        client = make_client(handler, usage_tracker=tracker, quota_tracker=quota)
        client.search(DiscoveryQuery(q="x"))  # 100 units
        with pytest.raises(QuotaExceededError):
            client.search(DiscoveryQuery(q="x"))  # would be 200 > 150
        assert len(calls) == 1  # the second call never hit the API

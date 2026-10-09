"""Market analysis engine unit tests.

Covers: metric normalization, age-adjusted calculations, engagement
calculations, channel-relative analysis, missing-metrics handling (never
invented), deterministic scoring, reproducibility, and empty input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from factory.intelligence.analysis import SCORING_WEIGHTS, AnalysisEngine
from factory.schemas.artifacts import (
    DiscoveredChannel,
    DiscoveredVideo,
    DiscoveryResult,
)

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def make_video(
    video_id: str,
    *,
    views: int = 1000,
    likes: int = 0,
    comments: int = 0,
    channel_id: str = "UC1",
    published_at: datetime | None = NOW - timedelta(days=10),
    duration_seconds: int | None = 600,
    title: str = "A video",
) -> DiscoveredVideo:
    return DiscoveredVideo(
        video_id=video_id,
        channel_id=channel_id,
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
        published_at=published_at,
        duration_seconds=duration_seconds,
        views=views,
        likes=likes,
        comments=comments,
    )


def make_channel(
    channel_id: str = "UC1", *, view_count: int = 10000, video_count: int = 10
) -> DiscoveredChannel:
    return DiscoveredChannel(
        channel_id=channel_id,
        title=f"Channel {channel_id}",
        view_count=view_count,
        video_count=video_count,
    )


def make_discovery(
    videos: list[DiscoveredVideo], channels: list[DiscoveredChannel] | None = None
) -> DiscoveryResult:
    return DiscoveryResult(
        discovery_id="d1",
        project_id="p1",
        query="q",
        videos=videos,
        channels=channels or [],
    )


class TestMetricCalculation:
    def test_age_adjusted_velocity(self) -> None:
        video = make_video("v1", views=1000, published_at=NOW - timedelta(days=10))
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([video]), project_id="p1", analysis_id="a1"
        )
        analyzed = payload["videos"][0]
        assert analyzed["age_days"] == pytest.approx(10.0)
        assert analyzed["views_per_day"] == pytest.approx(100.0)

    def test_velocity_none_for_unknown_or_same_day(self) -> None:
        unknown = make_video("v1", views=100, published_at=None)
        same_day = make_video("v2", views=100, published_at=NOW - timedelta(hours=2))
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([unknown, same_day]), project_id="p1", analysis_id="a1"
        )
        by_id = {v["video_id"]: v for v in payload["videos"]}
        assert by_id["v1"]["views_per_day"] is None
        assert by_id["v1"]["age_days"] is None
        assert by_id["v2"]["views_per_day"] is None  # age < 1 day → not estimated

    def test_engagement_calculations(self) -> None:
        video = make_video("v1", views=1000, likes=30, comments=20)
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([video]), project_id="p1", analysis_id="a1"
        )
        analyzed = payload["videos"][0]
        assert analyzed["engagement_rate"] == pytest.approx(0.05)
        assert analyzed["comments_per_1000_views"] == pytest.approx(20.0)

    def test_engagement_none_when_no_views(self) -> None:
        video = make_video("v1", views=0, likes=0, comments=0)
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([video]), project_id="p1", analysis_id="a1"
        )
        assert payload["videos"][0]["engagement_rate"] is None

    def test_channel_relative_performance(self) -> None:
        # Channel average = 10000 / 10 = 1000 views per video.
        video = make_video("v1", views=3000, channel_id="UC1")
        other = make_video("v2", views=500, channel_id="UC2")
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery(
                [video, other],
                [make_channel("UC1"), make_channel("UC2", view_count=5000, video_count=10)],
            ),
            project_id="p1",
            analysis_id="a1",
        )
        by_id = {v["video_id"]: v for v in payload["videos"]}
        assert by_id["v1"]["channel_average_views"] == pytest.approx(1000.0)
        assert by_id["v1"]["channel_relative_performance"] == pytest.approx(3.0)
        assert by_id["v2"]["channel_relative_performance"] == pytest.approx(1.0)

    def test_channel_relative_none_without_channel_data(self) -> None:
        video = make_video("v1", views=3000, channel_id="UCX")
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([video]), project_id="p1", analysis_id="a1"
        )
        assert payload["videos"][0]["channel_relative_performance"] is None


class TestScoring:
    def test_composite_uses_available_sub_scores(self) -> None:
        # Two videos: one strong on everything, one weak.
        strong = make_video(
            "strong", views=10000, likes=500, comments=100, published_at=NOW - timedelta(days=1)
        )
        weak = make_video(
            "weak", views=100, likes=1, comments=0, published_at=NOW - timedelta(days=100)
        )
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([strong, weak], [make_channel("UC1")]),
            project_id="p1",
            analysis_id="a1",
        )
        by_id = {v["video_id"]: v for v in payload["videos"]}
        assert by_id["strong"]["composite_score"] > by_id["weak"]["composite_score"]
        assert 0.0 <= by_id["weak"]["composite_score"] <= 100.0
        # Sub-scores are present and normalized:
        for sub in ("performance", "velocity", "engagement", "channel_relative", "recency"):
            assert sub in by_id["strong"]["sub_scores"]
            assert 0.0 <= by_id["strong"]["sub_scores"][sub] <= 1.0

    def test_missing_metrics_exclude_sub_scores_and_renormalize(self) -> None:
        # No published_at, no channels → velocity/recency/channel_relative absent.
        video = make_video("v1", views=100, published_at=None)
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([video]), project_id="p1", analysis_id="a1"
        )
        analyzed = payload["videos"][0]
        assert "velocity" not in analyzed["sub_scores"]
        assert "recency" not in analyzed["sub_scores"]
        assert "channel_relative" not in analyzed["sub_scores"]
        assert "performance" in analyzed["sub_scores"]
        # Composite still computed from the available sub-scores:
        assert analyzed["composite_score"] >= 0.0

    def test_weights_sum_to_one_and_are_documented(self) -> None:
        assert sum(SCORING_WEIGHTS.values()) == pytest.approx(1.0)
        video = make_video("v1", views=100)
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([video]), project_id="p1", analysis_id="a1"
        )
        assert payload["scoring"]["weights"] == SCORING_WEIGHTS
        assert "composite" in payload["scoring"]["formulas"]
        assert "weights" in payload["scoring"]

    def test_top_performers_ranked(self) -> None:
        videos = [
            make_video("low", views=10),
            make_video(
                "high", views=9999, likes=900, comments=90, published_at=NOW - timedelta(days=1)
            ),
            make_video("mid", views=500),
        ]
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery(videos), project_id="p1", analysis_id="a1"
        )
        assert payload["top_performer_video_ids"][0] == "high"

    def test_aggregates_and_competition(self) -> None:
        videos = [
            make_video("v1", views=100, channel_id="UC1"),
            make_video("v2", views=300, channel_id="UC1"),
            make_video("v3", views=600, channel_id="UC2"),
        ]
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery(videos), project_id="p1", analysis_id="a1"
        )
        assert payload["video_count"] == 3
        assert payload["total_views"] == 1000
        assert payload["average_views"] == pytest.approx(1000 / 3)
        assert payload["median_views"] == pytest.approx(300.0)
        assert payload["channel_count"] == 2
        competition = payload["competition"]
        assert competition["channel_count"] == 2
        assert competition["average_views_per_channel"] == pytest.approx(500.0)
        top_share = competition["top_channel_view_share"]
        assert top_share == pytest.approx(600 / 1000)  # UC2: 600 of 1000 views

    def test_patterns_extracted(self) -> None:
        videos = [
            make_video("v1", title="The lost tunnels of Rome"),
            make_video("v2", title="The lost tunnels of Egypt"),
            make_video("v3", title="What is the lost tunnels mystery?"),
        ]
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery(videos), project_id="p1", analysis_id="a1"
        )
        pattern_kinds = {p["pattern_type"] for p in payload["patterns"]}
        assert "topic" in pattern_kinds
        assert "question" in pattern_kinds
        topics = [p["pattern"] for p in payload["patterns"] if p["pattern_type"] == "topic"]
        assert "lost tunnels" in topics

    def test_reproducible(self) -> None:
        videos = [
            make_video("v1", views=100, likes=5, comments=2, published_at=NOW - timedelta(days=3)),
            make_video(
                "v2", views=9000, likes=400, comments=50, published_at=NOW - timedelta(days=30)
            ),
        ]
        discovery = make_discovery(videos, [make_channel("UC1")])
        first = AnalysisEngine(now=NOW).analyze(discovery, project_id="p1", analysis_id="a1")
        second = AnalysisEngine(now=NOW).analyze(discovery, project_id="p1", analysis_id="a1")
        assert first == second

    def test_empty_input(self) -> None:
        payload = AnalysisEngine(now=NOW).analyze(
            make_discovery([]), project_id="p1", analysis_id="a1"
        )
        assert payload["video_count"] == 0
        assert payload["videos"] == []
        assert payload["average_views"] == 0.0
        assert payload["competition"]["channel_count"] == 0

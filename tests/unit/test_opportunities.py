"""Opportunity detection unit tests.

Covers: evidence references, scoring, confidence, saturation signals,
novelty/gap reasoning, empty/low-quality markets, deterministic ids, and the
critical rule — opportunities never copy competitor content.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from factory.intelligence.analysis import AnalysisEngine
from factory.intelligence.opportunities import OPPORTUNITY_SCORE_WEIGHTS, OpportunityEngine
from factory.schemas.artifacts import (
    AnalysisResult,
    DiscoveredChannel,
    DiscoveredVideo,
    DiscoveryResult,
)

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def make_discovery() -> DiscoveryResult:
    """A mixed market: an underserved recurring theme + established topics."""
    videos = [
        # Underserved theme: recurring, low views.
        DiscoveredVideo(
            video_id="aq1",
            channel_id="UC1",
            title="The ancient aqueducts of Rome",
            url="https://www.youtube.com/watch?v=aq1",
            views=1200,
            likes=40,
            comments=5,
            published_at=NOW - timedelta(days=30),
            duration_seconds=900,
        ),
        DiscoveredVideo(
            video_id="aq2",
            channel_id="UC2",
            title="Ancient aqueducts: the water mystery",
            url="https://www.youtube.com/watch?v=aq2",
            views=900,
            likes=30,
            comments=3,
            published_at=NOW - timedelta(days=60),
            duration_seconds=1200,
        ),
        DiscoveredVideo(
            video_id="aq3",
            channel_id="UC3",
            title="What happened to the ancient aqueducts?",
            url="https://www.youtube.com/watch?v=aq3",
            views=1500,
            likes=50,
            comments=8,
            published_at=NOW - timedelta(days=90),
            duration_seconds=600,
        ),
        # Established topic: recurring, high views.
        DiscoveredVideo(
            video_id="tn1",
            channel_id="UC1",
            title="The forgotten tunnels of Paris",
            url="https://www.youtube.com/watch?v=tn1",
            views=800_000,
            likes=40_000,
            comments=3_000,
            published_at=NOW - timedelta(days=20),
            duration_seconds=1500,
        ),
        DiscoveredVideo(
            video_id="tn2",
            channel_id="UC2",
            title="Forgotten tunnels under the city",
            url="https://www.youtube.com/watch?v=tn2",
            views=650_000,
            likes=30_000,
            comments=2_000,
            published_at=NOW - timedelta(days=45),
            duration_seconds=1800,
        ),
        DiscoveredVideo(
            video_id="tn3",
            channel_id="UC4",
            title="The tunnels nobody talks about",
            url="https://www.youtube.com/watch?v=tn3",
            views=720_000,
            likes=35_000,
            comments=2_500,
            published_at=NOW - timedelta(days=70),
            duration_seconds=1200,
        ),
    ]
    channels = [
        DiscoveredChannel(channel_id="UC1", title="C1", view_count=2_000_000, video_count=40),
        DiscoveredChannel(channel_id="UC2", title="C2", view_count=1_800_000, video_count=36),
        DiscoveredChannel(channel_id="UC3", title="C3", view_count=900_000, video_count=18),
        DiscoveredChannel(channel_id="UC4", title="C4", view_count=1_500_000, video_count=30),
    ]
    return DiscoveryResult(
        discovery_id="d1",
        project_id="p1",
        query="history mystery",
        videos=videos,
        channels=channels,
    )


def make_analysis() -> AnalysisResult:
    payload = AnalysisEngine(now=NOW).analyze(make_discovery(), project_id="p1", analysis_id="a1")
    return AnalysisResult.model_validate(payload)


class TestOpportunityGeneration:
    def test_underserved_theme_surfaces_with_full_evidence(self) -> None:
        opportunities = OpportunityEngine().generate(
            make_analysis(), project_id="p1", opportunity_list_id="o1"
        )
        assert opportunities["opportunities"]
        by_topic = {o["topic"]: o for o in opportunities["opportunities"]}
        assert "ancient aqueducts" in by_topic
        opp = by_topic["ancient aqueducts"]
        # Evidence references + supporting videos:
        assert opp["evidence_refs"]
        assert opp["supporting_video_ids"]
        assert set(opp["supporting_video_ids"]) == {"aq1", "aq2", "aq3"}
        # Signals:
        assert opp["demand_signals"]["supporting_video_count"] == 3
        assert opp["competition_signals"]["saturation"] == "underserved"
        # Reasoning:
        assert opp["novelty_rationale"]
        assert opp["rationale"]
        assert opp["recommended_angle"]
        assert opp["audience_question"]
        assert opp["content_gap"]
        # Confidence in bounds:
        assert 0.0 < opp["confidence"] <= 1.0
        # Score in bounds:
        assert 0.0 <= opp["score"] <= 100.0

    def test_underserved_scores_above_saturated(self) -> None:
        opportunities = OpportunityEngine().generate(
            make_analysis(), project_id="p1", opportunity_list_id="o1"
        )
        by_topic = {o["topic"]: o for o in opportunities["opportunities"]}
        assert by_topic["ancient aqueducts"]["score"] > by_topic["forgotten tunnels"]["score"]

    def test_scores_sorted_and_limited(self) -> None:
        opportunities = OpportunityEngine(max_opportunities=3).generate(
            make_analysis(), project_id="p1", opportunity_list_id="o1"
        )
        scores = [o["score"] for o in opportunities["opportunities"]]
        assert scores == sorted(scores, reverse=True)
        assert len(opportunities["opportunities"]) <= 3

    def test_deterministic_ids(self) -> None:
        first = OpportunityEngine().generate(
            make_analysis(), project_id="p1", opportunity_list_id="o1"
        )
        second = OpportunityEngine().generate(
            make_analysis(), project_id="p1", opportunity_list_id="o1"
        )
        ids_first = [o["opportunity_id"] for o in first["opportunities"]]
        ids_second = [o["opportunity_id"] for o in second["opportunities"]]
        assert ids_first == ids_second
        assert all(ids_first)
        # Same project+topic → same id; different project → different id:
        other = OpportunityEngine().generate(
            make_analysis(), project_id="p2", opportunity_list_id="o1"
        )
        assert [o["opportunity_id"] for o in other["opportunities"]] != ids_first

    def test_reproducible_output(self) -> None:
        analysis = make_analysis()
        first = OpportunityEngine().generate(
            analysis, project_id="p1", opportunity_list_id="o1", now=NOW
        )
        second = OpportunityEngine().generate(
            analysis, project_id="p1", opportunity_list_id="o1", now=NOW
        )
        assert first == second

    def test_clusters_included(self) -> None:
        opportunities = OpportunityEngine().generate(
            make_analysis(), project_id="p1", opportunity_list_id="o1"
        )
        assert opportunities["clusters"]
        for cluster in opportunities["clusters"]:
            assert cluster["cluster_id"]
            assert cluster["label"]
            assert cluster["saturation"] in ("underserved", "balanced", "saturated")

    def test_source_artifact_linked(self) -> None:
        opportunities = OpportunityEngine().generate(
            make_analysis(), project_id="p1", opportunity_list_id="o1", source_artifact_id="art-1"
        )
        assert opportunities["source_artifact_id"] == "art-1"


class TestNoCopying:
    def test_opportunity_text_never_contains_competitor_content(self) -> None:
        """The critical rule: opportunities identify gaps — they never rewrite
        or imitate competitor videos. Opportunity text is generated from topic
        terms and measured signals only."""
        analysis = make_analysis()
        competitor_titles = {v.title for v in analysis.videos}
        competitor_descriptions = {
            "A documentary about ancient aqueducts.",  # would be description text
        }
        opportunities = OpportunityEngine().generate(
            analysis, project_id="p1", opportunity_list_id="o1"
        )
        for opp in opportunities["opportunities"]:
            text_fields = [
                opp["title"],
                opp["rationale"],
                opp["novelty_rationale"],
                opp["recommended_angle"],
                opp["content_gap"],
                opp["audience_question"],
            ]
            for text in text_fields:
                assert text
                for competitor_title in competitor_titles:
                    assert competitor_title not in text
                for description in competitor_descriptions:
                    assert description not in text


class TestEmptyAndLowQualityMarkets:
    def test_empty_market_yields_no_opportunities(self) -> None:
        empty = AnalysisResult(analysis_id="a1", project_id="p1", video_count=0)
        opportunities = OpportunityEngine().generate(
            empty, project_id="p1", opportunity_list_id="o1"
        )
        assert opportunities["opportunities"] == []
        assert opportunities["clusters"] == []

    def test_single_video_market(self) -> None:
        discovery = DiscoveryResult(
            discovery_id="d1",
            project_id="p1",
            query="q",
            videos=[
                DiscoveredVideo(
                    video_id="only",
                    channel_id="UC1",
                    title="A unique video about quantum history",
                    url="https://www.youtube.com/watch?v=only",
                    views=10,
                )
            ],
        )
        payload = AnalysisEngine(now=NOW).analyze(discovery, project_id="p1", analysis_id="a1")
        analysis = AnalysisResult.model_validate(payload)
        opportunities = OpportunityEngine().generate(
            analysis, project_id="p1", opportunity_list_id="o1"
        )
        # No recurring topics (frequency < 2) → no opportunities, gracefully:
        assert opportunities["opportunities"] == []

    def test_weights_sum_to_one(self) -> None:
        assert sum(OPPORTUNITY_SCORE_WEIGHTS.values()) == pytest.approx(1.0)

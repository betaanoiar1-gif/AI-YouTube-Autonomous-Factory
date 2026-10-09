"""Opportunity detection engine (OPPORTUNITY_DETECTION job).

Generates structured, evidence-based content opportunities from an
``analysis_result`` artifact plus the deterministic clustering over its videos.

Critical rule: the system identifies opportunities — it does NOT rewrite or
imitate competitor videos. All opportunity text is generated from topic terms
and measured signals only; no competitor title, description, or narration is
ever copied into an opportunity.

Scoring (documented in each opportunity's signals and reproducible):

* demand (40%) — total views of the videos supporting the topic, normalized;
* gap (30%) — inverse competition: lower average views than the sample median
  means more room to stand out;
* novelty (20%) — the topic is classified underserved by the clusterer;
* confidence (10%) — evidence volume and data completeness.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from factory.intelligence.clustering import (
    ClusteringResult,
    DeterministicClusterer,
    tokenize,
)
from factory.schemas.artifacts import AnalysisResult, OpportunityList

#: Opportunity score weights.
OPPORTUNITY_SCORE_WEIGHTS: dict[str, float] = {
    "demand": 0.40,
    "gap": 0.30,
    "novelty": 0.20,
    "confidence": 0.10,
}

#: Supporting videos needed for full confidence.
FULL_CONFIDENCE_SUPPORT = 5


class OpportunityEngine:
    """Deterministic opportunity generation (no LLM dependency)."""

    def __init__(
        self,
        *,
        max_opportunities: int = 10,
        min_topic_frequency: int = 2,
        clusterer: DeterministicClusterer | None = None,
    ) -> None:
        self._max_opportunities = max_opportunities
        self._min_topic_frequency = min_topic_frequency
        self._clusterer = clusterer or DeterministicClusterer()

    def generate(
        self,
        analysis: AnalysisResult,
        *,
        project_id: str,
        opportunity_list_id: str,
        source_artifact_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Generate the opportunity_list payload from an analysis result."""
        videos = list(analysis.videos)
        clustering: ClusteringResult = self._clusterer.cluster(
            videos, min_topic_frequency=self._min_topic_frequency
        )

        # Candidate topics: underserved first (gap-driven), then balanced ones.
        candidate_topics: list[str] = list(clustering.underserved_topics)
        for occurrence in clustering.topics:
            if occurrence.topic not in candidate_topics:
                candidate_topics.append(occurrence.topic)

        # Video lookup helpers.
        by_id = {v.video_id: v for v in videos}
        median_views = analysis.median_views or 0.0

        opportunities: list[dict[str, Any]] = []
        for topic in candidate_topics:
            opportunity = self._build_opportunity(
                topic=topic,
                analysis=analysis,
                clustering=clustering,
                by_id=by_id,
                median_views=median_views,
                project_id=project_id,
            )
            if opportunity is not None:
                opportunities.append(opportunity)

        # Deterministic order: score desc, then topic asc.
        opportunities.sort(key=lambda o: (-o["score"], o["topic"]))
        opportunities = opportunities[: self._max_opportunities]

        clusters = [
            {
                "cluster_id": cluster.cluster_id,
                "label": cluster.label,
                "video_count": len(cluster.video_ids),
                "total_views": sum(by_id[vid].views for vid in cluster.video_ids if vid in by_id),
                "average_views": (
                    sum(by_id[vid].views for vid in cluster.video_ids if vid in by_id)
                    / len(cluster.video_ids)
                    if cluster.video_ids
                    else 0.0
                ),
                "saturation": (
                    clustering.saturation.get(cluster.label, {}).get("saturation", "balanced")
                ),
                "representative_video_ids": cluster.video_ids[:5],
            }
            for cluster in clustering.clusters
        ]

        payload = OpportunityList(
            opportunity_list_id=opportunity_list_id,
            project_id=project_id,
            generated_at=now or datetime.now(UTC),
            opportunities=opportunities,
            source_artifact_id=source_artifact_id,
            clusters=clusters,
        )
        return payload.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_opportunity(
        self,
        *,
        topic: str,
        analysis: AnalysisResult,
        clustering: ClusteringResult,
        by_id: dict[str, Any],
        median_views: float,
        project_id: str,
    ) -> dict[str, Any] | None:
        topic_tokens = set(tokenize(topic))
        if not topic_tokens:
            return None

        # Supporting videos: those whose title contains all topic tokens.
        supporting: list[Any] = []
        for video in analysis.videos:
            title_tokens = set(tokenize(video.title))
            if topic_tokens and topic_tokens <= title_tokens:
                supporting.append(video)
        if not supporting:
            return None

        supporting_ids = sorted(v.video_id for v in supporting)
        total_views = sum(v.views for v in supporting)
        average_views = total_views / len(supporting)
        velocities = [v.views_per_day for v in supporting if v.views_per_day is not None]
        average_velocity = sum(velocities) / len(velocities) if velocities else None
        channel_ids = {v.channel_id for v in supporting}

        # --- signals ---
        saturation_info = clustering.saturation.get(topic, {})
        saturation = saturation_info.get("saturation", "balanced")
        demand_signals = {
            "topic_frequency": len(supporting),
            "supporting_video_count": len(supporting),
            "total_views": total_views,
            "average_views": average_views,
            "average_views_per_day": average_velocity,
        }
        competition_signals = {
            "video_count": len(supporting),
            "channel_count": len(channel_ids),
            "average_views": average_views,
            "sample_median_views": median_views,
            "saturation": saturation,
        }

        # --- confidence: evidence volume + data completeness ---
        videos_with_dates = sum(1 for v in supporting if v.published_at is not None)
        completeness = videos_with_dates / len(supporting)
        evidence_factor = min(len(supporting) / FULL_CONFIDENCE_SUPPORT, 1.0)
        confidence = round(min(evidence_factor * (0.5 + 0.5 * completeness), 1.0), 4)

        # --- sub-scores (0..1) ---
        sample_max_views = max((v.views for v in analysis.videos), default=0)
        demand_score = min(total_views / sample_max_views, 1.0) if sample_max_views else 0.0
        if median_views > 0:
            gap_score = max(1.0 - min(average_views / median_views, 1.0), 0.0)
        else:
            gap_score = 0.5
        novelty_score = 1.0 if saturation == "underserved" else 0.0
        confidence_score = confidence

        weight_sum = sum(OPPORTUNITY_SCORE_WEIGHTS.values())
        score = round(
            100.0
            * (
                OPPORTUNITY_SCORE_WEIGHTS["demand"] * demand_score
                + OPPORTUNITY_SCORE_WEIGHTS["gap"] * gap_score
                + OPPORTUNITY_SCORE_WEIGHTS["novelty"] * novelty_score
                + OPPORTUNITY_SCORE_WEIGHTS["confidence"] * confidence_score
            )
            / weight_sum,
            4,
        )

        # --- text (generated from topic terms + signals only; no copying) ---
        audience_question = self._audience_question(topic, clustering)
        novelty_rationale = (
            f"'{topic}' appears in {len(supporting)} analyzed videos "
            f"(average {int(average_views)} views vs sample median {int(median_views)}); "
            f"the clusterer classifies it as {saturation}, so original coverage has "
            f"measurable room to outperform existing videos."
        )
        recommended_angle = (
            f"Produce an original, evidence-based video on '{topic}' for the target "
            f"audience: answer '{audience_question}' with primary sources and a "
            f"distinct narrative structure, rather than reacting to existing coverage."
        )
        rationale = (
            f"Demand is measurable ({len(supporting)} supporting videos, "
            f"{total_views} combined views) while competition is relatively low "
            f"(average {int(average_views)} views, {len(channel_ids)} channels, "
            f"saturation: {saturation})."
        )
        content_gap = (
            f"No analyzed video fully answers '{audience_question}' with original "
            f"primary-source coverage."
        )

        return {
            "opportunity_id": uuid.uuid5(
                uuid.NAMESPACE_URL, f"ai-youtube-autonomous-factory:{project_id}:{topic}"
            ).hex,
            "title": f"Original coverage: {topic}",
            "topic": topic,
            "score": score,
            "rationale": rationale,
            "content_gap": content_gap,
            "audience_question": audience_question,
            "evidence_refs": [
                f"analysis:{analysis.analysis_id}:topic:{topic}",
                *[f"video:{vid}" for vid in supporting_ids[:5]],
            ],
            "supporting_video_ids": supporting_ids,
            "demand_signals": demand_signals,
            "competition_signals": competition_signals,
            "novelty_rationale": novelty_rationale,
            "confidence": confidence,
            "recommended_angle": recommended_angle,
        }

    @staticmethod
    def _audience_question(topic: str, clustering: ClusteringResult) -> str:
        """The audience problem/question, GENERATED from the topic term.

        Repeated competitor questions are used as analysis evidence only —
        opportunity text never quotes a competitor title (the system identifies
        opportunities; it does not rewrite or imitate competitor videos).
        """
        topic_tokens = set(tokenize(topic))
        starters = [
            question.split()[0].rstrip("?").lower()
            for question in clustering.questions
            if topic_tokens and topic_tokens <= set(tokenize(question))
        ]
        starter = next(
            (s for s in starters if s in ("what", "how", "why", "when", "where", "who")),
            "what",
        )
        if starter == "how":
            return f"How does {topic} work, and why does it matter?"
        if starter == "why":
            return f"Why does {topic} matter to viewers today?"
        return f"What should viewers know about {topic}?"

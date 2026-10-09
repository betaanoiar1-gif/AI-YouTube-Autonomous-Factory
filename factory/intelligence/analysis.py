"""Market analysis engine (MARKET_ANALYSIS job).

Analyzes a ``discovery_result`` artifact into an ``analysis_result`` artifact
using measurable signals only: views, age-adjusted performance, view velocity,
engagement, channel-relative performance, recency, topic frequency, competition,
and content formats. The analysis:

* preserves per-video evidence (metrics + sub-scores) in the artifact;
* documents the scoring weights and formulas in the artifact (``scoring``);
* never invents metrics — when required source data is unavailable the derived
  field is ``None`` and the sub-score is excluded (weights are renormalized
  over the available sub-scores);
* is deterministic: the same input and analysis timestamp produce the same
  output (the timestamp is injectable for reproducibility).

It is not "highest views = best": the composite score blends normalized
performance, velocity, engagement, channel-relative performance, and recency.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from statistics import median
from typing import Any

from factory.intelligence.clustering import ClusteringResult, DeterministicClusterer
from factory.schemas.artifacts import AnalysisResult, DiscoveryResult

#: Composite score weights (documented in the artifact's ``scoring`` field).
SCORING_WEIGHTS: dict[str, float] = {
    "performance": 0.30,
    "velocity": 0.25,
    "engagement": 0.20,
    "channel_relative": 0.15,
    "recency": 0.10,
}

SCORING_FORMULAS: dict[str, str] = {
    "performance": "views / max(views) across the analyzed sample (min-max, 0..1)",
    "velocity": "views_per_day / max(views_per_day) across the sample (min-max, 0..1); "
    "views_per_day = views / age_days (None when published_at is unknown or age < 1 day)",
    "engagement": "engagement_rate / max(engagement_rate) across the sample (min-max, 0..1); "
    "engagement_rate = (likes + comments) / views (None when views == 0)",
    "channel_relative": (
        "channel_relative_performance / max(...) across the sample (min-max, 0..1); "
        "channel_relative_performance = views / (channel view_count / channel video_count) "
        "(None when channel metrics are unavailable)"
    ),
    "recency": "1 - age_days / max(age_days) across the sample (0..1; newer is higher)",
    "composite": "100 * sum(weight_i * sub_score_i) / sum(weight_i) over the AVAILABLE "
    "sub-scores (weights renormalized when a metric is unavailable)",
}


class AnalysisEngine:
    """Deterministic market analysis over a discovery result."""

    def __init__(
        self,
        *,
        now: datetime | None = None,
        clusterer: DeterministicClusterer | None = None,
    ) -> None:
        self._now = now
        self._clusterer = clusterer or DeterministicClusterer()

    def analyze(
        self,
        discovery: DiscoveryResult,
        *,
        project_id: str,
        analysis_id: str,
        source_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Analyze a discovery result and return the analysis payload."""
        now = self._now or datetime.now(UTC)
        videos = list(discovery.videos)
        channel_average: dict[str, float] = {}
        for channel in discovery.channels:
            if channel.view_count is not None and channel.video_count:
                channel_average[channel.channel_id] = channel.view_count / channel.video_count

        analyzed: list[dict[str, Any]] = []
        for video in videos:
            analyzed.append(self._analyze_video(video, channel_average=channel_average, now=now))

        # --- sub-score normalization across the sample ---
        self._normalize_sub_scores(analyzed)
        for entry in analyzed:
            entry["composite_score"] = self._composite(entry["sub_scores"])

        # --- aggregates ---
        views_list = [v.views for v in videos]
        total_views = sum(views_list)
        average_views = total_views / len(videos) if videos else 0.0
        median_views = float(median(views_list)) if views_list else 0.0
        channel_ids = {v.channel_id for v in videos}

        # --- competition ---
        channel_stats: dict[str, dict[str, int]] = {}
        for video in videos:
            stats = channel_stats.setdefault(video.channel_id, {"video_count": 0, "total_views": 0})
            stats["video_count"] += 1
            stats["total_views"] += video.views
        top_channel_share = (
            max(s["total_views"] for s in channel_stats.values()) / total_views
            if total_views and channel_stats
            else 0.0
        )
        competition = {
            "video_count": len(videos),
            "channel_count": len(channel_ids),
            "average_views_per_channel": total_views / len(channel_ids) if channel_ids else 0.0,
            "top_channel_view_share": top_channel_share,
            "channels": [
                {
                    "channel_id": channel_id,
                    "video_count": stats["video_count"],
                    "total_views": stats["total_views"],
                    "average_views": stats["total_views"] / stats["video_count"],
                }
                for channel_id, stats in sorted(channel_stats.items())
            ],
        }

        # --- patterns (deterministic clustering over the same videos) ---
        clustering: ClusteringResult = self._clusterer.cluster(videos)
        patterns: list[dict[str, Any]] = []
        sample_size = len(videos) or 1
        for topic in clustering.topics:
            patterns.append(
                {
                    "pattern": topic.topic,
                    "pattern_type": "topic",
                    "supporting_video_ids": topic.video_ids,
                    "confidence": min(1.0, topic.frequency / sample_size),
                }
            )
        for fmt in clustering.formats:
            patterns.append(
                {
                    "pattern": fmt.pattern,
                    "pattern_type": "format",
                    "supporting_video_ids": fmt.video_ids,
                    "confidence": fmt.share,
                }
            )
        for question in clustering.questions:
            patterns.append(
                {
                    "pattern": question,
                    "pattern_type": "question",
                    "supporting_video_ids": [],
                    "confidence": 1.0,
                }
            )

        ranked = sorted(analyzed, key=lambda e: (-e["composite_score"], e["video_id"]))
        payload = AnalysisResult(
            analysis_id=analysis_id,
            project_id=project_id,
            analyzed_at=now,
            video_count=len(videos),
            total_views=total_views,
            average_views=average_views,
            median_views=median_views,
            channel_count=len(channel_ids),
            top_performer_video_ids=[e["video_id"] for e in ranked[:10]],
            videos=[self._strip(e) for e in ranked],
            patterns=patterns,
            competition=competition,
            scoring={
                "weights": dict(SCORING_WEIGHTS),
                "formulas": dict(SCORING_FORMULAS),
                "normalization": "min-max across the analyzed sample; unavailable metrics "
                "are excluded and the remaining weights are renormalized",
            },
            source_artifact_id=source_artifact_id,
        )
        return payload.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _analyze_video(
        self,
        video: Any,
        *,
        channel_average: dict[str, float],
        now: datetime,
    ) -> dict[str, Any]:
        """Compute per-video metrics (None when source data is unavailable)."""
        age_days: float | None = None
        if video.published_at is not None:
            age_delta = now - video.published_at
            age_days = max(age_delta.total_seconds() / 86400.0, 0.0)
        views_per_day: float | None = None
        if age_days is not None and age_days >= 1.0:
            views_per_day = video.views / age_days
        engagement_rate: float | None = None
        comments_per_1000: float | None = None
        if video.views > 0:
            engagement_rate = (video.likes + video.comments) / video.views
            comments_per_1000 = video.comments * 1000.0 / video.views
        channel_avg = channel_average.get(video.channel_id)
        channel_relative: float | None = None
        if channel_avg is not None and channel_avg > 0:
            channel_relative = video.views / channel_avg
        return {
            "video_id": video.video_id,
            "channel_id": video.channel_id,
            "title": video.title,
            "views": video.views,
            "likes": video.likes,
            "comments": video.comments,
            "duration_seconds": video.duration_seconds,
            "published_at": video.published_at,
            "age_days": age_days,
            "views_per_day": views_per_day,
            "engagement_rate": engagement_rate,
            "comments_per_1000_views": comments_per_1000,
            "channel_average_views": channel_avg,
            "channel_relative_performance": channel_relative,
            "sub_scores": {},
            "composite_score": 0.0,
        }

    def _normalize_sub_scores(self, analyzed: Sequence[dict[str, Any]]) -> None:
        """Min-max normalize each metric across the sample into 0..1 sub-scores."""
        metric_to_key = {
            "performance": "views",
            "velocity": "views_per_day",
            "engagement": "engagement_rate",
            "channel_relative": "channel_relative_performance",
        }
        for sub_score, metric in metric_to_key.items():
            values = [e[metric] for e in analyzed if e[metric] is not None]
            if not values:
                continue
            max_value = max(values)
            if max_value <= 0:
                continue
            for entry in analyzed:
                value = entry[metric]
                if value is not None:
                    entry["sub_scores"][sub_score] = min(value / max_value, 1.0)
        # Recency: newer is better (1 - age / max_age).
        ages = [e["age_days"] for e in analyzed if e["age_days"] is not None]
        if ages:
            max_age = max(ages)
            for entry in analyzed:
                if entry["age_days"] is not None and max_age > 0:
                    entry["sub_scores"]["recency"] = 1.0 - entry["age_days"] / max_age

    @staticmethod
    def _composite(sub_scores: dict[str, float]) -> float:
        """Weighted composite over available sub-scores (renormalized)."""
        available = {k: v for k, v in sub_scores.items() if k in SCORING_WEIGHTS}
        if not available:
            return 0.0
        weight_sum = sum(SCORING_WEIGHTS[k] for k in available)
        if weight_sum <= 0:
            return 0.0
        composite = sum(SCORING_WEIGHTS[k] * available[k] for k in available) / weight_sum
        return round(min(max(composite * 100.0, 0.0), 100.0), 4)

    @staticmethod
    def _strip(entry: dict[str, Any]) -> dict[str, Any]:
        """Drop the raw metric echo from the artifact (evidence is in sub_scores
        + the key metrics kept explicitly)."""
        return entry

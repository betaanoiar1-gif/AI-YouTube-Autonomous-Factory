"""Deterministic, provider-independent topic/pattern clustering.

This module is the clustering foundation for the intelligence plane. It is
deliberately deterministic (same input → same output) and free of any LLM
dependency: topics are recurring n-grams, clusters are connected components
over shared topics, and saturation is derived from measurable signals only.

A future embedding/LLM-based clusterer can implement the :class:`Clusterer`
protocol and replace :class:`DeterministicClusterer` without changing callers.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

#: Common English stopwords (deterministic, embedded — no external data).
STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all am an and any are aren't as at be
    because been before being below between both but by can can't cannot could
    couldn't did didn't do does doesn't doing don't down during each few for
    from further had hadn't has hasn't have haven't having he he'd he'll he's
    her here here's hers herself him himself his how how's i i'd i'll i'm i've
    if in into is isn't it it's its itself let's me more most mustn't my myself
    no nor not of off on once only or other ought our ours ourselves out over
    own same shan't she she'd she'll she's should shouldn't so some such than
    that that's the their theirs them themselves then there there's these they
    they'd they'll they're they've this those through to too under until up
    very was wasn't we we'd we'll we're we've were weren't what what's when
    when's where where's which while who who's whom why why's will with won't
    would wouldn't you you'd you'll you're you've your yours yourself yourselves
    """.split()  # noqa: SIM905 - compact embedded stopword list
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_QUESTION_START_RE = re.compile(
    r"^(who|what|when|where|why|how|which|is|are|was|were|do|does|did|can|could|"
    r"should|would|shall|may|might|must)\b",
    re.IGNORECASE,
)

#: Duration buckets in seconds (analysis parameters, not API values).
DURATION_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("short_under_5m", 300),
    ("medium_5m_20m", 1200),
    ("long_over_20m", None),
)

#: Title format patterns detected deterministically.
FORMAT_PATTERNS: dict[str, re.Pattern[str]] = {
    "has_number": re.compile(r"\d"),
    "has_bracket": re.compile(r"[\[\(]"),
    "has_vs": re.compile(r"\bvs\.?\b", re.IGNORECASE),
    "has_pipe": re.compile(r"\|"),
    "has_question_mark": re.compile(r"\?"),
    "has_colon": re.compile(r":"),
}


@runtime_checkable
class ClusterableVideo(Protocol):
    """The minimal video shape the clusterer needs (artifact or provider model)."""

    video_id: str
    title: str
    views: int
    published_at: Any


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords and short tokens removed."""
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= 3 and token not in STOPWORDS
    ]


def extract_ngrams(tokens: Sequence[str], n: int) -> list[str]:
    """Join consecutive tokens into n-grams (deterministic order)."""
    if n <= 1:
        return list(tokens)
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


class TopicOccurrence(BaseModel):
    """A recurring topic (n-gram) and the videos containing it."""

    topic: str = Field(min_length=1, max_length=200)
    frequency: int = Field(ge=1)
    video_ids: list[str] = Field(default_factory=list)


class FormatPattern(BaseModel):
    """A recurring content-format pattern and its prevalence."""

    pattern: str = Field(min_length=1, max_length=64)
    video_count: int = Field(ge=0)
    video_ids: list[str] = Field(default_factory=list)
    share: float = Field(ge=0.0, le=1.0)


class TopicCluster(BaseModel):
    """A deterministic cluster of videos linked by shared recurring topics."""

    cluster_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    video_ids: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class ClusteringResult(BaseModel):
    """The output of the clustering layer (input to opportunity detection)."""

    topics: list[TopicOccurrence] = Field(default_factory=list)
    clusters: list[TopicCluster] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    formats: list[FormatPattern] = Field(default_factory=list)
    underserved_topics: list[str] = Field(default_factory=list)
    #: topic -> {video_count, total_views, average_views, saturation}
    saturation: dict[str, dict[str, Any]] = Field(default_factory=dict)


@runtime_checkable
class Clusterer(Protocol):
    """Protocol for clustering implementations (deterministic or learned)."""

    def cluster(
        self, videos: Sequence[ClusterableVideo], *, min_topic_frequency: int = 2
    ) -> ClusteringResult: ...


class DeterministicClusterer:
    """Deterministic, provider-independent clusterer (no LLM dependency)."""

    def __init__(
        self,
        *,
        max_topics: int = 40,
        saturation_video_share: float = 0.5,
        min_cluster_size: int = 2,
        underserved_ratio: float = 0.5,
    ) -> None:
        self._max_topics = max_topics
        self._saturation_video_share = saturation_video_share
        self._min_cluster_size = min_cluster_size
        #: A topic is "underserved" when its average views are below this
        #: fraction of the sample median — a real gap, not sampling noise.
        self._underserved_ratio = underserved_ratio

    def cluster(
        self, videos: Sequence[ClusterableVideo], *, min_topic_frequency: int = 2
    ) -> ClusteringResult:
        """Cluster videos by recurring topics; derive questions, formats,
        underserved themes, and saturation indicators. Fully deterministic."""
        # Deduplicate by video id so duplicated input is handled gracefully.
        seen_ids: set[str] = set()
        video_list = []
        for video in videos:
            if video.video_id not in seen_ids:
                seen_ids.add(video.video_id)
                video_list.append(video)
        if not video_list:
            return ClusteringResult()

        # --- recurring topics (unigrams + bigrams) ---
        topic_videos: dict[str, list[str]] = {}
        for video in video_list:
            tokens = tokenize(video.title)
            grams = extract_ngrams(tokens, 1) + extract_ngrams(tokens, 2)
            for gram in set(grams):
                topic_videos.setdefault(gram, []).append(video.video_id)
        topics = [
            TopicOccurrence(
                topic=topic,
                frequency=len(video_ids),
                video_ids=sorted(video_ids),
            )
            for topic, video_ids in topic_videos.items()
            if len(video_ids) >= min_topic_frequency
        ]
        # Deterministic order: frequency desc, then topic asc.
        topics.sort(key=lambda t: (-t.frequency, t.topic))
        topics = topics[: self._max_topics]

        # --- clusters: connected components over shared topics ---
        topic_set = {t.topic for t in topics}
        video_topics: dict[str, set[str]] = {}
        for video in video_list:
            title_tokens = set(tokenize(video.title))
            video_topics[video.video_id] = {
                topic for topic in topic_set if all(part in title_tokens for part in topic.split())
            }
        clusters = self._connected_components(video_topics, topics)

        # --- repeated questions ---
        questions = sorted(
            {
                video.title.strip()
                for video in video_list
                if video.title.strip().endswith("?") or _QUESTION_START_RE.match(video.title)
            }
        )

        # --- recurring formats ---
        formats = self._format_patterns(video_list)

        # --- saturation + underserved themes ---
        views_by_id = {v.video_id: v.views for v in video_list}
        sorted_views = sorted(views_by_id.values())
        median_views = (
            sorted_views[len(sorted_views) // 2]
            if len(sorted_views) % 2 == 1
            else (sorted_views[len(sorted_views) // 2 - 1] + sorted_views[len(sorted_views) // 2])
            / 2
        )
        saturation: dict[str, dict[str, Any]] = {}
        underserved: list[str] = []
        saturation_threshold = max(
            self._min_cluster_size,
            int(len(video_list) * self._saturation_video_share + 0.999),
        )
        for topic in topics:
            supporting = topic.video_ids
            total_views = sum(views_by_id.get(vid, 0) for vid in supporting)
            average_views = total_views / len(supporting) if supporting else 0.0
            if average_views < median_views * self._underserved_ratio:
                classification = "underserved"
                underserved.append(topic.topic)
            elif len(supporting) >= saturation_threshold and average_views >= median_views:
                classification = "saturated"
            else:
                classification = "balanced"
            saturation[topic.topic] = {
                "video_count": len(supporting),
                "total_views": total_views,
                "average_views": average_views,
                "saturation": classification,
                "median_views_sample": median_views,
            }

        return ClusteringResult(
            topics=topics,
            clusters=clusters,
            questions=questions,
            formats=formats,
            underserved_topics=sorted(underserved),
            saturation=saturation,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connected_components(
        self,
        video_topics: dict[str, set[str]],
        topics: list[TopicOccurrence],
    ) -> list[TopicCluster]:
        """Group videos into connected components linked by shared topics."""
        topic_frequency = {t.topic: t.frequency for t in topics}
        # Union-find over video ids.
        parent: dict[str, str] = {vid: vid for vid in video_topics}

        def find(vid: str) -> str:
            while parent[vid] != vid:
                parent[vid] = parent[parent[vid]]
                vid = parent[vid]
            return vid

        def union(a: str, b: str) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        by_topic: dict[str, list[str]] = {}
        for vid, vid_topics in video_topics.items():
            for topic in vid_topics:
                by_topic.setdefault(topic, []).append(vid)
        for vids in by_topic.values():
            for other in vids[1:]:
                union(vids[0], other)

        components: dict[str, list[str]] = {}
        for vid in video_topics:
            components.setdefault(find(vid), []).append(vid)

        clusters: list[TopicCluster] = []
        for members in components.values():
            if len(members) < self._min_cluster_size:
                continue
            members = sorted(members)
            shared_topics = sorted(
                {topic for vid in members for topic in video_topics[vid]},
                key=lambda t: (-topic_frequency.get(t, 0), t),
            )
            if not shared_topics:
                continue
            label = shared_topics[0]
            cluster_id = hashlib.sha256("|".join(members).encode("utf-8")).hexdigest()[:12]
            clusters.append(
                TopicCluster(
                    cluster_id=cluster_id,
                    label=label,
                    video_ids=members,
                    topics=shared_topics[:5],
                )
            )
        clusters.sort(key=lambda c: (-len(c.video_ids), c.cluster_id))
        return clusters

    def _format_patterns(self, videos: Sequence[ClusterableVideo]) -> list[FormatPattern]:
        """Recurring format patterns: duration buckets + title formats."""
        total = len(videos)
        patterns: list[FormatPattern] = []

        bucket_counts: Counter[str] = Counter()
        bucket_videos: dict[str, list[str]] = {}
        for video in videos:
            duration = getattr(video, "duration_seconds", None)
            bucket = None
            if duration is not None:
                for name, limit in DURATION_BUCKETS:
                    if limit is None or duration < limit:
                        bucket = name
                        break
            if bucket is not None:
                bucket_counts[bucket] += 1
                bucket_videos.setdefault(bucket, []).append(video.video_id)
        for bucket, count in sorted(bucket_counts.items()):
            patterns.append(
                FormatPattern(
                    pattern=f"duration:{bucket}",
                    video_count=count,
                    video_ids=sorted(bucket_videos[bucket]),
                    share=count / total,
                )
            )

        for name, regex in sorted(FORMAT_PATTERNS.items()):
            vids = sorted(v.video_id for v in videos if regex.search(v.title))
            if vids:
                patterns.append(
                    FormatPattern(
                        pattern=f"title:{name}",
                        video_count=len(vids),
                        video_ids=vids,
                        share=len(vids) / total,
                    )
                )
        return patterns

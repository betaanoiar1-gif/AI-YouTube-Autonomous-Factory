"""Deterministic clustering unit tests.

Covers: recurring topics, deterministic behavior, empty input, duplicate
input, insufficient data, questions, formats, underserved/saturation
classification, and the Clusterer protocol (replaceable by a learned
implementation).
"""

from __future__ import annotations

from datetime import UTC, datetime

from factory.intelligence.clustering import (
    Clusterer,
    DeterministicClusterer,
    TopicOccurrence,
    extract_ngrams,
    tokenize,
)


class FakeVideo:
    """Minimal video shape for the clusterer (artifact/provider duck type)."""

    def __init__(
        self, video_id: str, title: str, views: int, duration_seconds: int | None = None
    ) -> None:
        self.video_id = video_id
        self.title = title
        self.views = views
        self.duration_seconds = duration_seconds
        self.published_at: datetime | None = datetime(2024, 6, 1, tzinfo=UTC)


def videos(*specs: tuple[str, str, int]) -> list[FakeVideo]:
    return [FakeVideo(vid, title, views) for vid, title, views in specs]


class TestTokenize:
    def test_lowercase_and_stopwords_removed(self) -> None:
        assert tokenize("The Lost Tunnels of Rome") == ["lost", "tunnels", "rome"]

    def test_short_tokens_removed(self) -> None:
        assert tokenize("a an ox go to") == []

    def test_ngrams(self) -> None:
        assert extract_ngrams(["a", "b", "c"], 2) == ["a b", "b c"]
        assert extract_ngrams(["a"], 1) == ["a"]


class TestRecurringTopics:
    def test_recurring_topics_detected(self) -> None:
        vs = videos(
            ("1", "The lost tunnels of Rome", 100),
            ("2", "The lost tunnels of Egypt", 200),
            ("3", "Lost tunnels: what happened?", 300),
            ("4", "Unrelated topic entirely", 400),
        )
        result = DeterministicClusterer().cluster(vs, min_topic_frequency=2)
        topics = {t.topic: t.frequency for t in result.topics}
        assert topics["lost tunnels"] == 3
        assert "unrelated" not in topics  # below the frequency threshold

    def test_insufficient_data_yields_no_topics(self) -> None:
        vs = videos(("1", "alpha beta", 10), ("2", "gamma delta", 20))
        result = DeterministicClusterer().cluster(vs, min_topic_frequency=2)
        assert result.topics == []
        assert result.underserved_topics == []

    def test_empty_input(self) -> None:
        result = DeterministicClusterer().cluster([])
        assert result.topics == []
        assert result.clusters == []
        assert result.questions == []
        assert result.formats == []
        assert result.saturation == {}

    def test_duplicate_input_is_idempotent(self) -> None:
        vs = videos(
            ("1", "The lost tunnels of Rome", 100),
            ("2", "The lost tunnels of Egypt", 200),
        )
        clusterer = DeterministicClusterer()
        first = clusterer.cluster(vs)
        second = clusterer.cluster(vs)
        assert first == second
        # Duplicated videos produce the same clusters:
        doubled = clusterer.cluster(vs + vs)
        assert doubled.topics == first.topics

    def test_deterministic_ordering(self) -> None:
        vs = videos(
            ("1", "zebra tunnels mystery", 10),
            ("2", "zebra tunnels history", 20),
            ("3", "alpha tunnels mystery", 30),
            ("4", "alpha tunnels history", 40),
        )
        result = DeterministicClusterer().cluster(vs, min_topic_frequency=2)
        topics = [t.topic for t in result.topics]
        # Equal frequencies are tie-broken alphabetically:
        frequencies = {x.topic: x.frequency for x in result.topics}
        assert topics == sorted(topics, key=lambda t: (-frequencies[t], t))


class TestQuestionsAndFormats:
    def test_questions_detected(self) -> None:
        vs = videos(
            ("1", "What is the lost tunnel?", 10),
            ("2", "How did the tunnel collapse", 20),
            ("3", "A statement title", 30),
        )
        result = DeterministicClusterer().cluster(vs)
        assert "What is the lost tunnel?" in result.questions
        assert "How did the tunnel collapse" in result.questions
        assert "A statement title" not in result.questions

    def test_format_patterns(self) -> None:
        vs = [
            FakeVideo("1", "Top 10 tunnels", 10, duration_seconds=240),
            FakeVideo("2", "Top 5 mysteries", 20, duration_seconds=240),
            FakeVideo("3", "A [deep] dive", 30, duration_seconds=2400),
        ]
        result = DeterministicClusterer().cluster(vs)
        patterns = {f.pattern: f for f in result.formats}
        assert patterns["duration:short_under_5m"].video_count == 2
        assert patterns["duration:long_over_20m"].video_count == 1
        assert patterns["title:has_number"].video_count == 2
        assert patterns["title:has_bracket"].video_count == 1
        assert "title:has_question_mark" not in patterns  # zero-match patterns omitted


class TestSaturation:
    def test_underserved_and_saturated_classification(self) -> None:
        # "rare topic" recurs with LOW views (underserved); "hot topic"
        # recurs with HIGH views and covers most of the sample (saturated).
        vs = [
            FakeVideo("1", "rare topic alpha", 100),
            FakeVideo("2", "rare topic beta", 150),
            FakeVideo("3", "rare topic gamma", 120),
            FakeVideo("4", "hot topic alpha", 900_000),
            FakeVideo("5", "hot topic beta", 950_000),
            FakeVideo("6", "hot topic gamma", 920_000),
            FakeVideo("7", "hot topic delta", 930_000),
            FakeVideo("8", "hot topic epsilon", 910_000),
            FakeVideo("9", "hot topic zeta", 940_000),
        ]
        result = DeterministicClusterer(min_cluster_size=2).cluster(vs, min_topic_frequency=2)
        assert "rare topic" in result.underserved_topics
        assert result.saturation["rare topic"]["saturation"] == "underserved"
        assert result.saturation["hot topic"]["saturation"] == "saturated"

    def test_balanced_classification(self) -> None:
        # Recurring but below the saturation share threshold (2 of 6 < 50%)
        # and near the median → balanced.
        vs = videos(
            ("1", "shared topic alpha", 400_000),
            ("2", "shared topic beta", 420_000),
            ("3", "other stuff one", 380_000),
            ("4", "other stuff two", 410_000),
            ("5", "more stuff three", 390_000),
            ("6", "more stuff four", 405_000),
        )
        result = DeterministicClusterer().cluster(vs, min_topic_frequency=2)
        assert result.saturation["shared topic"]["saturation"] == "balanced"


class TestClusters:
    def test_connected_components(self) -> None:
        vs = videos(
            ("1", "tunnels of rome", 10),
            ("2", "tunnels of egypt", 20),
            ("3", "rome history facts", 30),
            ("4", "unrelated subject", 40),
        )
        result = DeterministicClusterer().cluster(vs, min_topic_frequency=2)
        # 1-2-3 link through shared topics; 4 is alone (min cluster size 2).
        labels = {c.label: set(c.video_ids) for c in result.clusters}
        assert any({"1", "2", "3"} <= members for members in labels.values())
        assert all("4" not in members for members in labels.values())

    def test_cluster_ids_deterministic(self) -> None:
        vs = videos(("1", "tunnels of rome", 10), ("2", "tunnels of egypt", 20))
        first = DeterministicClusterer().cluster(vs)
        second = DeterministicClusterer().cluster(vs)
        assert [c.cluster_id for c in first.clusters] == [c.cluster_id for c in second.clusters]


class TestProtocol:
    def test_clusterer_protocol(self) -> None:
        clusterer = DeterministicClusterer()
        assert isinstance(clusterer, Clusterer)
        result = clusterer.cluster(
            videos(("1", "alpha beta gamma", 10), ("2", "alpha beta delta", 20)),
            min_topic_frequency=2,
        )
        assert result.topics
        assert isinstance(result.topics[0], TopicOccurrence)

"""Deterministic evidence extractor (EvidenceExtractor implementation).

Converts collected source material into structured evidence with provenance
(source id, passage, location, confidence). Deterministic patterns extract
facts (dates, numbers) with structured subject/predicate/value fields for
contradiction detection; generic declarative sentences become lower-confidence
evidence. No LLM dependency (an LLM-backed extractor can implement the same
contract later — optionally via the existing LLMProvider abstraction).

Only bounded passages are stored — never large copies of source material.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from factory.config.research_config import ResearchSettings, get_research_settings
from factory.providers.research.base import EvidenceExtractor
from factory.providers.research.types import CollectedSource
from factory.schemas.artifacts import EvidenceItem, ResearchPlan

#: Maximum passage length stored per evidence item.
MAX_PASSAGE_CHARS = 2000

_YEAR_RE = re.compile(r"\b((?:1[0-9]|20)\d{2})\b")
_NUMBER_RE = re.compile(r"\b(\d[\d,]{2,}(?:\.\d+)?)\b")
_FACT_VERBS = (
    "was",
    "were",
    "is",
    "are",
    "completed",
    "built",
    "founded",
    "established",
    "began",
    "opened",
    "created",
    "carried",
    "carries",
    "ruled",
    "lived",
    "died",
    "served",
    "supplied",
    "transported",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CAPITALIZED_RE = re.compile(r"^[A-Z][A-Za-z0-9'&.\- ]{1,60}")


class _TextExtractor(HTMLParser):
    """Minimal HTML → text converter (stdlib only).

    Skips ``title``/``script``/``style`` content so page metadata does not
    pollute the extracted subject material.
    """

    _SKIP_TAGS = frozenset({"title", "script", "style", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return " ".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    with contextlib.suppress(Exception):
        # Malformed HTML still yields partial text.
        parser.feed(html)
        parser.close()
    return parser.get_text()


def _extract_subject(sentence: str) -> str | None:
    """Leading capitalized noun phrase (heuristic subject extraction)."""
    match = _CAPITALIZED_RE.match(sentence.strip())
    if not match:
        return None
    phrase = match.group(0).strip(" ,;:")
    words = phrase.split()
    # Keep at most the first 4 words and stop before a fact verb.
    kept: list[str] = []
    for word in words[:4]:
        if word.lower() in _FACT_VERBS:
            break
        kept.append(word)
    # Strip leading articles for stable subject grouping.
    while kept and kept[0].lower() in ("the", "a", "an"):
        kept.pop(0)
    if not kept:
        return None
    return " ".join(kept).lower()


def _extract_predicate(sentence: str) -> str | None:
    """The verb phrase after the subject, stopping before any value token.

    Value-free predicates are essential: two sources reporting different
    values for the same fact must land in the SAME (subject, predicate)
    group so contradiction detection can compare the values.
    """
    words = sentence.split()
    for index, word in enumerate(words):
        if word.lower().strip(",.;:") in _FACT_VERBS:
            phrase: list[str] = []
            for following in words[index : index + 4]:
                token = following.strip(",.;:")
                if _YEAR_RE.fullmatch(token) or _NUMBER_RE.fullmatch(token):
                    break  # stop before the value
                phrase.append(token)
            if not phrase:
                return None
            return " ".join(phrase).lower()
    return None


class DeterministicEvidenceExtractor(EvidenceExtractor):
    """Deterministic pattern-based evidence extraction."""

    name = "deterministic-extractor"

    def __init__(self, *, settings: ResearchSettings | None = None) -> None:
        self._settings = settings or get_research_settings()

    def extract_evidence(
        self,
        document: CollectedSource,
        *,
        plan: ResearchPlan | None = None,
        job_id: str | None = None,
    ) -> list[EvidenceItem]:
        if not document.content:
            return []
        text = document.content
        if (document.content_type or "").startswith("text/html"):
            text = html_to_text(text)
        if not text.strip():
            return []

        source_id = f"src-{document.candidate.url_fingerprint[:16]}"
        evidence: list[EvidenceItem] = []
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        for index, sentence in enumerate(sentences):
            if len(evidence) >= self._settings.max_evidence_per_source:
                break
            item = self._extract_from_sentence(sentence, source_id, index)
            if item is not None:
                evidence.append(item)
        return evidence

    def _extract_from_sentence(
        self, sentence: str, source_id: str, index: int
    ) -> EvidenceItem | None:
        if not (20 <= len(sentence) <= 600):
            return None
        if sentence.strip().endswith("?"):
            return None  # questions are not evidence
        subject = _extract_subject(sentence)
        predicate = _extract_predicate(sentence)

        year_match = _YEAR_RE.search(sentence)
        number_match = _NUMBER_RE.search(sentence)
        if year_match and predicate:
            value, value_type, confidence = year_match.group(1), "date", 0.9
        elif number_match and predicate:
            value, value_type, confidence = number_match.group(1), "number", 0.85
        elif predicate and subject:
            value, value_type, confidence = None, "text", 0.5
        else:
            return None

        claim = sentence.strip()
        if len(claim) > MAX_PASSAGE_CHARS:
            claim = claim[: MAX_PASSAGE_CHARS - 3] + "..."
        return EvidenceItem(
            evidence_id=f"ev-{source_id}-{index:04d}",
            source_id=source_id,
            claim=claim,
            passage=claim,
            location=f"sentence {index + 1}",
            confidence=confidence,
            extracted_at=datetime.now(UTC),
            subject=subject,
            predicate=predicate,
            value=value,
            value_type=value_type,
        )

"""Evidence extractor unit tests: patterns, provenance, confidence, limits."""

from __future__ import annotations

from datetime import UTC, datetime

from factory.providers.research.types import CollectedSource
from factory.research.extractor import DeterministicEvidenceExtractor, html_to_text
from factory.research.sources import make_candidate


def _document(
    text: str, *, source_type: str = "reference", content_type: str = "text/plain"
) -> CollectedSource:
    candidate = make_candidate(
        "https://example.com/page",
        title="Page",
        hint=source_type,
    )
    return CollectedSource(
        candidate=candidate,
        content=text,
        content_type=content_type,
        byte_size=len(text),
        content_fingerprint="f" * 64,
        collection_status="collected",
        collected_at=datetime.now(UTC),
    )


class TestHtmlToText:
    def test_strips_tags_and_skips_title(self) -> None:
        html = (
            "<html><head><title>Meta Title</title></head>"
            "<body><p>Hello <b>world</b></p></body></html>"
        )
        text = html_to_text(html)
        assert "Hello world" in text
        assert "Meta Title" not in text

    def test_malformed_html_tolerated(self) -> None:
        assert "some text" in html_to_text("<p>some text")


class TestExtraction:
    def test_date_fact(self) -> None:
        items = DeterministicEvidenceExtractor().extract_evidence(
            _document("The Aqua Aqueduct was completed in 1312.")
        )
        assert len(items) == 1
        item = items[0]
        assert item.subject == "aqua aqueduct"
        assert item.predicate == "was completed in"
        assert item.value == "1312"
        assert item.value_type == "date"
        assert item.confidence == 0.9
        assert item.location == "sentence 1"
        assert item.source_id.startswith("src-")

    def test_number_fact(self) -> None:
        items = DeterministicEvidenceExtractor().extract_evidence(
            _document("The Aqua Aqueduct carried 250,000 cubic meters of water per day.")
        )
        assert len(items) == 1
        assert items[0].value == "250,000"
        assert items[0].value_type == "number"
        assert items[0].predicate == "carried"

    def test_text_fact(self) -> None:
        items = DeterministicEvidenceExtractor().extract_evidence(
            _document("The Aqua Aqueduct was built by the Roman Empire.")
        )
        assert len(items) == 1
        assert items[0].value_type == "text"
        assert items[0].value is None
        assert items[0].confidence == 0.5

    def test_predicate_is_value_free(self) -> None:
        """Two sources with different values must share the predicate."""
        extractor = DeterministicEvidenceExtractor()
        a = extractor.extract_evidence(_document("The Aqua Aqueduct was completed in 1312."))
        b = extractor.extract_evidence(_document("The Aqua Aqueduct was completed in 1305."))
        assert a[0].predicate == b[0].predicate
        assert a[0].value != b[0].value

    def test_html_content_extracted(self) -> None:
        items = DeterministicEvidenceExtractor().extract_evidence(
            _document(
                "<html><body><p>The Aqua Aqueduct was completed in 1312.</p></body></html>",
                content_type="text/html",
            )
        )
        assert len(items) == 1
        assert items[0].value == "1312"

    def test_questions_are_not_evidence(self) -> None:
        items = DeterministicEvidenceExtractor().extract_evidence(
            _document("Was the Aqua Aqueduct really completed in 1312?")
        )
        assert items == []

    def test_short_or_long_sentences_skipped(self) -> None:
        extractor = DeterministicEvidenceExtractor()
        assert extractor.extract_evidence(_document("Too short.")) == []
        long_sentence = "The Aqua Aqueduct was completed in 1312 and " + "x" * 700
        assert extractor.extract_evidence(_document(long_sentence)) == []

    def test_empty_content(self) -> None:
        assert DeterministicEvidenceExtractor().extract_evidence(_document("")) == []

    def test_passage_within_contract_bound(self) -> None:
        items = DeterministicEvidenceExtractor().extract_evidence(
            _document("The Aqua Aqueduct was completed in 1312.")
        )
        assert items
        # The passage is the (bounded) sentence — within the contract's limit:
        assert len(items[0].passage) <= 2000

    def test_evidence_per_source_capped(self) -> None:
        text = " ".join(
            f"The Aqua Aqueduct fact number {i} was completed in 1312." for i in range(50)
        )
        items = DeterministicEvidenceExtractor().extract_evidence(_document(text))
        assert 0 < len(items) <= 25

    def test_provenance_fields(self) -> None:
        items = DeterministicEvidenceExtractor().extract_evidence(
            _document("First sentence here. The Aqua Aqueduct was completed in 1312.")
        )
        assert items
        item = items[0]
        assert item.evidence_id
        assert item.source_id
        assert item.passage
        assert item.extracted_at is not None

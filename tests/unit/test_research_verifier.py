"""Claim verifier unit tests: statuses, independence, syndication, authority,
contradictions. Never ``number of sources = truth``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factory.providers.research.types import ClaimDraft, CollectedSource, EvidenceDraft
from factory.research.sources import make_candidate
from factory.research.verifier import DeterministicClaimVerifier
from factory.security.fetch import content_fingerprint


def _source(url: str, content: str = "", *, authority_hint: str = "reference") -> CollectedSource:
    candidate = make_candidate(url, title="T", hint=authority_hint)
    return CollectedSource(
        candidate=candidate,
        content=content,
        content_type="text/plain",
        byte_size=len(content),
        content_fingerprint=content_fingerprint(content) if content else None,
        collection_status="collected",
        collected_at=datetime.now(UTC),
    )


def _source_id(source: CollectedSource) -> str:
    return f"src-{source.candidate.url_fingerprint[:16]}"


def _evidence(
    source_id: str, value: str | None, *, value_type: str | None = "date"
) -> EvidenceDraft:
    return EvidenceDraft(
        source_id=source_id,
        claim=f"claim {value}",
        passage=f"passage {value}",
        location="sentence 1",
        confidence=0.9,
        subject="aqua aqueduct",
        predicate="was completed in",
        value=value,
        value_type=value_type,
    )


def _claim(
    sources: list[CollectedSource],
    values: list[str | None],
    *,
    value: str | None = None,
    statement: str = "The aqua aqueduct was completed in 1312.",
    subject: str | None = "aqua aqueduct",
    predicate: str | None = "was completed in",
    value_type: str | None = "date",
) -> ClaimDraft:
    """Build a claim whose evidence references the given sources' real ids."""
    evidence = [
        _evidence(_source_id(source), v, value_type=value_type)
        for source, v in zip(sources, values, strict=True)
    ]
    return ClaimDraft(
        statement=statement,
        claim_type="fact",
        importance="high",
        evidence=evidence,
        subject=subject,
        predicate=predicate,
        value=value if value is not None else (values[0] if values else None),
        value_type=value_type,
    )


class TestVerificationStatuses:
    def test_supported_single_source(self) -> None:
        sources = [_source("https://a.example/x", "The Aqua Aqueduct was completed in 1312.")]
        claim = _claim(sources, ["1312"])
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        assert result.verification_status == "SUPPORTED"
        assert result.independent_source_count == 1

    def test_multi_source_supported(self) -> None:
        sources = [
            _source("https://a.example/x", "text one"),
            _source("https://b.example/y", "text two"),
        ]
        claim = _claim(sources, ["1312", "1312"])
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        assert result.verification_status == "MULTI_SOURCE_SUPPORTED"
        assert result.independent_source_count == 2

    def test_syndicated_copies_are_not_independent(self) -> None:
        """Copies of the same article are not independent confirmations."""
        content = "The Aqua Aqueduct was completed in 1312."
        sources = [
            _source("https://a.example/x", content),
            _source("https://b.example/y", content),  # syndicated copy
            _source("https://c.example/z", content),  # syndicated copy
        ]
        claim = _claim(sources, ["1312"] * 3)
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        assert result.independent_source_count == 1
        assert result.verification_status == "SUPPORTED"  # not MULTI_SOURCE

    def test_contested_when_sources_disagree(self) -> None:
        sources = [
            _source("https://a.example/x", "text one"),
            _source("https://b.example/y", "text two"),
            _source("https://c.example/z", "text three"),
        ]
        claim = _claim(sources, ["1312", "1312", "1305"], value="1312")
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        assert result.verification_status == "CONTESTED"
        assert result.contradicting_source_ids
        assert result.supporting_source_ids
        assert result.notes

    def test_contradicted_when_only_contradicting(self) -> None:
        sources = [_source("https://a.example/x", "text one")]
        claim = _claim(sources, ["1305"], value="1312")
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        assert result.verification_status == "CONTRADICTED"

    def test_insufficient_evidence(self) -> None:
        claim = ClaimDraft(statement="Something.", evidence=[])
        result = DeterministicClaimVerifier().verify_claims([claim], sources=[])[0]
        assert result.verification_status == "INSUFFICIENT_EVIDENCE"
        # Absence of evidence is never confirmation:
        assert result.confidence == 0.0

    def test_confidence_formula_components(self) -> None:
        sources = [
            _source("https://a.example/x", "text one", authority_hint="academic"),
            _source("https://b.example/y", "text two", authority_hint="government"),
        ]
        claim = _claim(sources, ["1312", "1312"])
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        # coverage 1.0 x independence 1.0 x authority (0.9 -> 0.95):
        assert result.confidence == pytest.approx(0.95, abs=0.01)
        assert result.metadata["authority"] == 0.9


class TestContradictions:
    def test_conflicting_dates(self) -> None:
        sources = [
            _source("https://a.example/x", "one"),
            _source("https://b.example/y", "two"),
        ]
        claim = _claim(sources, ["1312", "1305"], value="1312")
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        date_conflicts = [c for c in result.contradictions if c.contradiction_type == "date"]
        assert date_conflicts
        assert set(date_conflicts[0].values) == {"1312", "1305"}
        assert len(date_conflicts[0].source_ids) == 2

    def test_conflicting_numbers(self) -> None:
        sources = [
            _source("https://a.example/x", "one"),
            _source("https://b.example/y", "two"),
        ]
        claim = _claim(
            sources,
            ["250,000", "300,000"],
            value="250,000",
            statement="The Aqua Aqueduct carried 250,000 cubic meters.",
            predicate="carried",
            value_type="number",
        )
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        number_conflicts = [c for c in result.contradictions if c.contradiction_type == "number"]
        assert number_conflicts
        assert set(number_conflicts[0].values) == {"250,000", "300,000"}

    def test_explicit_disagreement_detected(self) -> None:
        source = _source("https://a.example/x", "one")
        claim = ClaimDraft(
            statement="The Aqua Aqueduct was completed in 1305.",
            evidence=[
                EvidenceDraft(
                    source_id=_source_id(source),
                    claim="claim",
                    passage="However, other sources dispute this date.",
                    confidence=0.9,
                    subject="aqua aqueduct",
                    predicate="was completed in",
                    value="1305",
                    value_type="date",
                )
            ],
            subject="aqua aqueduct",
            predicate="was completed in",
            value="1305",
            value_type="date",
        )
        result = DeterministicClaimVerifier().verify_claims([claim], sources=[source])[0]
        disagreement = [c for c in result.contradictions if c.contradiction_type == "disagreement"]
        assert disagreement

    def test_no_contradiction_when_values_agree(self) -> None:
        sources = [
            _source("https://a.example/x", "one"),
            _source("https://b.example/y", "two"),
        ]
        claim = _claim(sources, ["1312", "1312"])
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        assert result.contradictions == []

    def test_both_sides_preserved(self) -> None:
        """The contradiction record keeps every source — no silent choice."""
        sources = [
            _source("https://a.example/x", "one"),
            _source("https://b.example/y", "two"),
        ]
        claim = _claim(sources, ["1312", "1305"], value="1312")
        result = DeterministicClaimVerifier().verify_claims([claim], sources=sources)[0]
        conflict = result.contradictions[0]
        assert set(conflict.source_ids) == {_source_id(sources[0]), _source_id(sources[1])}
        assert set(conflict.values) == {"1312", "1305"}

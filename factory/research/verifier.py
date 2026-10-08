"""Deterministic claim verifier (ClaimVerifier implementation).

Cross-source verification foundations:

* **Source independence** — supporting sources are counted by distinct
  registered domain; syndicated duplicates (identical content fingerprints)
  are NOT independent confirmations — ``number of sources != truth``;
* **Evidence coverage** — supporting vs contradicting evidence ratio;
* **Authority weighting** — government/academic/primary sources weigh more
  (documented, deterministic);
* **Confidence** — a published formula combining coverage, independence, and
  authority;
* **Contradiction detection** — conflicting numbers, dates, and explicit
  disagreement markers; both sides and their provenance are preserved; the
  system never silently chooses a side.

Absence of evidence is never treated as confirmation.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from factory.providers.research.base import ClaimVerifier
from factory.providers.research.types import (
    ClaimDraft,
    CollectedSource,
    ContradictionFinding,
    VerificationResult,
)
from factory.security.urls import registered_domain

#: Minimum independent sources for MULTI_SOURCE_SUPPORTED.
MULTI_SOURCE_THRESHOLD = 2

#: Passage markers indicating explicit disagreement between sources.
_DISAGREEMENT_MARKERS = (
    "however",
    "contrary to",
    "in contrast",
    "disputed",
    "contested",
    "according to",
    "but other sources",
    "conflicting reports",
)
_DISAGREEMENT_RE = re.compile(
    "|".join(re.escape(marker) for marker in _DISAGREEMENT_MARKERS), re.IGNORECASE
)


def _values_conflict(a: str, b: str) -> bool:
    """True when two extracted values conflict (numbers or dates)."""
    if a == b:
        return False
    try:
        return abs(float(a.replace(",", "")) - float(b.replace(",", ""))) > 0.5
    except ValueError:
        return True  # non-numeric values that differ conflict as text


class DeterministicClaimVerifier(ClaimVerifier):
    """Deterministic cross-source verification and contradiction detection."""

    name = "deterministic-verifier"

    def verify_claims(
        self,
        claims: list[ClaimDraft],
        *,
        sources: list[CollectedSource],
        job_id: str | None = None,
    ) -> list[VerificationResult]:
        source_by_id = {f"src-{s.candidate.url_fingerprint[:16]}": s for s in sources}
        # Syndication groups: identical content fingerprints across sources.
        fingerprint_domains: dict[str, set[str]] = {}
        for source in sources:
            if source.content_fingerprint:
                domain = registered_domain(urlsplit(source.candidate.url).hostname or "")
                fingerprint_domains.setdefault(source.content_fingerprint, set()).add(domain)

        results: list[VerificationResult] = []
        for index, claim in enumerate(claims):
            claim_id = f"cl-{index:04d}"
            results.append(self._verify_claim(claim, claim_id, source_by_id, fingerprint_domains))
        return results

    def _verify_claim(
        self,
        claim: ClaimDraft,
        claim_id: str,
        source_by_id: dict[str, CollectedSource],
        fingerprint_domains: dict[str, set[str]],
    ) -> VerificationResult:
        supporting: list[str] = []  # evidence indexes supporting the claim value
        contradicting: list[str] = []  # evidence indexes with conflicting values
        for evidence in claim.evidence:
            if claim.value is not None and evidence.value is not None:
                if _values_conflict(evidence.value, claim.value):
                    contradicting.append(evidence.source_id)
                else:
                    supporting.append(evidence.source_id)
            else:
                supporting.append(evidence.source_id)

        supporting_source_ids = sorted(set(supporting))
        contradicting_source_ids = sorted(set(contradicting))
        total_evidence = len(supporting) + len(contradicting)
        coverage = len(supporting) / total_evidence if total_evidence else 0.0

        # Independence: distinct registered domains, with syndicated sources
        # (same content fingerprint) counted once.
        independent_domains: set[str] = set()
        counted_fingerprints: set[str] = set()
        for source_id in supporting_source_ids:
            source = source_by_id.get(source_id)
            if source is None:
                continue
            fingerprint = source.content_fingerprint
            if fingerprint and fingerprint in counted_fingerprints:
                continue  # syndicated duplicate — not an independent confirmation
            if fingerprint:
                counted_fingerprints.add(fingerprint)
            domain = registered_domain(urlsplit(source.candidate.url).hostname or "")
            if domain:
                independent_domains.add(domain)
        independent_count = len(independent_domains)

        # Authority: the strongest supporting source's justified authority.
        authority = 0.0
        for source_id in supporting_source_ids:
            source = source_by_id.get(source_id)
            if source is not None:
                authority = max(authority, source.candidate.authority_score)

        # Confidence (published formula):
        #   coverage x (0.5 + 0.5 x independence) x (0.5 + 0.5 x authority)
        independence_factor = min(independent_count / MULTI_SOURCE_THRESHOLD, 1.0)
        confidence = round(
            coverage * (0.5 + 0.5 * independence_factor) * (0.5 + 0.5 * authority), 4
        )

        # Contradiction findings for this claim's (subject, predicate) group.
        contradictions = self._detect_contradictions(claim, claim_id, source_by_id)

        # Verification status (absence of evidence is never confirmation).
        if contradicting and not supporting:
            status = "CONTRADICTED"
        elif contradicting and supporting:
            status = "CONTESTED"
        elif independent_count >= MULTI_SOURCE_THRESHOLD:
            status = "MULTI_SOURCE_SUPPORTED"
        elif supporting:
            status = "SUPPORTED"
        elif claim.evidence:
            status = "UNVERIFIED"
        else:
            status = "INSUFFICIENT_EVIDENCE"

        notes = None
        if status == "CONTESTED":
            notes = (
                f"Sources disagree: {len(supporting)} supporting vs "
                f"{len(contradicting)} contradicting evidence items; both sides preserved."
            )
        elif status == "CONTRADICTED":
            notes = "All located evidence contradicts this claim."
        elif status == "INSUFFICIENT_EVIDENCE":
            notes = "No evidence located; absence of evidence is not confirmation."

        return VerificationResult(
            claim_id=claim_id,
            verification_status=status,
            confidence=confidence,
            supporting_source_ids=supporting_source_ids,
            contradicting_source_ids=contradicting_source_ids,
            independent_source_count=independent_count,
            evidence_coverage=round(coverage, 4),
            notes=notes,
            contradictions=contradictions,
            metadata={
                "authority": authority,
                "supporting_evidence": len(supporting),
                "contradicting_evidence": len(contradicting),
            },
        )

    def _detect_contradictions(
        self,
        claim: ClaimDraft,
        claim_id: str,
        source_by_id: dict[str, CollectedSource],
    ) -> list[ContradictionFinding]:
        """Detect conflicting values and explicit disagreement for a claim."""
        findings: list[ContradictionFinding] = []
        if claim.subject is None or claim.predicate is None:
            return findings

        # Conflicting values within the claim's evidence.
        values: dict[str, list[str]] = {}
        for evidence in claim.evidence:
            if evidence.value is not None:
                values.setdefault(evidence.value, []).append(evidence.source_id)
        if len(values) >= 2:
            distinct = sorted(values)
            contradiction_type = (
                claim.value_type if claim.value_type in ("number", "date") else "description"
            )
            findings.append(
                ContradictionFinding(
                    contradiction_type=contradiction_type,
                    description=(
                        f"Sources report conflicting values for "
                        f"'{claim.subject}' ({claim.predicate}): {', '.join(distinct)}"
                    ),
                    values=distinct,
                    source_ids=sorted({sid for ids in values.values() for sid in ids}),
                    claim_refs=[claim_id],
                    subject=claim.subject,
                    predicate=claim.predicate,
                )
            )

        # Explicit disagreement markers in passages.
        for evidence in claim.evidence:
            if _DISAGREEMENT_RE.search(evidence.passage):
                findings.append(
                    ContradictionFinding(
                        contradiction_type="disagreement",
                        description=(
                            f"Source explicitly qualifies or disputes the claim about "
                            f"'{claim.subject}': passage contains disagreement language."
                        ),
                        values=[],
                        source_ids=[evidence.source_id],
                        claim_refs=[claim_id],
                        subject=claim.subject,
                        predicate=claim.predicate,
                    )
                )
                break  # one explicit-disagreement finding per claim is enough
        return findings

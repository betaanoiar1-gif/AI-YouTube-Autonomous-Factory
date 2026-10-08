"""Normalized, vendor-neutral research-plane models.

These shapes are what the research engine sees. Provider implementations
translate between these models and their own backends.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

#: Source types (Phase 2).
SOURCE_TYPES = (
    "primary",
    "government",
    "academic",
    "journalism",
    "reference",
    "secondary",
    "unknown",
)

#: Verification statuses (Phase 2).
VERIFICATION_STATUSES = (
    "UNVERIFIED",
    "SUPPORTED",
    "MULTI_SOURCE_SUPPORTED",
    "CONTESTED",
    "CONTRADICTED",
    "INSUFFICIENT_EVIDENCE",
)


class SourceCandidate(BaseModel):
    """A source found by discovery (not yet collected)."""

    url: str = Field(min_length=1, max_length=2000)
    canonical_url: str = Field(min_length=1, max_length=2000)
    url_fingerprint: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    publisher: str | None = Field(default=None, max_length=256)
    published_at: datetime | None = None
    source_type: str = Field(default="unknown", max_length=32)
    discovery_query: str | None = Field(default=None, max_length=500)
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    authority_score: float = Field(default=0.0, ge=0.0, le=1.0)
    authority_indicators: list[str] = Field(default_factory=list)


class CollectedSource(BaseModel):
    """A source after safe collection (content included, bounded)."""

    candidate: SourceCandidate
    content: str = ""
    content_type: str | None = Field(default=None, max_length=128)
    byte_size: int = Field(default=0, ge=0)
    content_fingerprint: str | None = Field(default=None, max_length=64)
    #: collected | cached | failed
    collection_status: str = Field(default="collected", max_length=32)
    collection_error: str | None = Field(default=None, max_length=500)
    collected_at: datetime | None = None
    redirects: list[str] = Field(default_factory=list)
    from_cache: bool = False


class EvidenceDraft(BaseModel):
    """A piece of evidence extracted from a collected source."""

    source_id: str = Field(min_length=1, max_length=64)
    claim: str = Field(min_length=1, max_length=1000)
    passage: str = Field(default="", max_length=2000)
    location: str | None = Field(default=None, max_length=128)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    subject: str | None = Field(default=None, max_length=200)
    predicate: str | None = Field(default=None, max_length=128)
    value: str | None = Field(default=None, max_length=128)
    #: number | date | text
    value_type: str | None = Field(default=None, max_length=16)


class ClaimDraft(BaseModel):
    """A claim built from evidence (before verification)."""

    statement: str = Field(min_length=1, max_length=1000)
    claim_type: str = Field(default="fact", max_length=32)
    importance: str = Field(default="medium", max_length=16)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    subject: str | None = Field(default=None, max_length=200)
    predicate: str | None = Field(default=None, max_length=128)
    value: str | None = Field(default=None, max_length=128)
    value_type: str | None = Field(default=None, max_length=16)


class ContradictionFinding(BaseModel):
    """A detected contradiction between sources."""

    contradiction_type: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=1000)
    values: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    claim_refs: list[str] = Field(default_factory=list)
    subject: str | None = Field(default=None, max_length=200)
    predicate: str | None = Field(default=None, max_length=128)


class VerificationResult(BaseModel):
    """The verification outcome for one claim."""

    claim_id: str = Field(min_length=1, max_length=64)
    verification_status: str = Field(default="UNVERIFIED", max_length=32)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    supporting_source_ids: list[str] = Field(default_factory=list)
    contradicting_source_ids: list[str] = Field(default_factory=list)
    independent_source_count: int = Field(default=0, ge=0)
    evidence_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str | None = Field(default=None, max_length=1000)
    contradictions: list[ContradictionFinding] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

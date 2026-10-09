"""Research-plane provider contracts (Phase 2).

Four provider-neutral interfaces keep the research engine decoupled from any
specific search engine, website, LLM, or retrieval implementation:

* :class:`ResearchProvider` — research planning (opportunity → plan);
* :class:`SourceProvider` — source discovery AND safe source collection;
* :class:`EvidenceExtractor` — collected documents → structured evidence;
* :class:`ClaimVerifier` — deterministic cross-source claim verification.

Future providers (other search backends, LLM-backed extraction via the
existing ``LLMProvider`` abstraction, learned verification) implement these
contracts without rewriting the research engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from factory.providers.research.types import (
    ClaimDraft,
    CollectedSource,
    SourceCandidate,
    VerificationResult,
)
from factory.schemas.artifacts import (
    EvidenceItem,
    OpportunityItem,
    ResearchPlan,
)


class ResearchProvider(ABC):
    """Research planning: derive a structured plan from an opportunity."""

    name: str = "research"

    @abstractmethod
    def plan_research(
        self,
        opportunity: OpportunityItem,
        *,
        project_id: str,
        depth: str = "standard",
        language: str | None = None,
        target_audience: str | None = None,
        job_id: str | None = None,
    ) -> ResearchPlan:
        """Produce a research plan for the opportunity (never copies
        competitor titles/descriptions — generated from the topic)."""
        raise NotImplementedError


class SourceProvider(ABC):
    """Source discovery and safe source collection."""

    name: str = "sources"

    @abstractmethod
    def discover_sources(
        self,
        query: str,
        *,
        context: str | None = None,
        limit: int = 10,
        source_types: list[str] | None = None,
        job_id: str | None = None,
    ) -> list[SourceCandidate]:
        """Find candidate sources for a research query."""
        raise NotImplementedError

    @abstractmethod
    def collect_source(
        self, source: SourceCandidate, *, job_id: str | None = None
    ) -> CollectedSource:
        """Safely retrieve a source (SSRF-protected, size/type limits).

        Sources that cannot be safely retrieved are returned with
        ``collection_status="failed"`` — content is never invented.
        """
        raise NotImplementedError


class EvidenceExtractor(ABC):
    """Extract structured evidence from collected source material."""

    name: str = "evidence"

    @abstractmethod
    def extract_evidence(
        self,
        document: CollectedSource,
        *,
        plan: ResearchPlan | None = None,
        job_id: str | None = None,
    ) -> list[EvidenceItem]:
        """Extract evidence items from a collected document."""
        raise NotImplementedError


class ClaimVerifier(ABC):
    """Deterministic cross-source claim verification."""

    name: str = "verification"

    @abstractmethod
    def verify_claims(
        self,
        claims: list[ClaimDraft],
        *,
        sources: list[CollectedSource],
        job_id: str | None = None,
    ) -> list[VerificationResult]:
        """Verify claims against evidence and sources.

        Must account for source independence (syndicated copies are not
        independent confirmations) — ``number of sources != truth``.
        """
        raise NotImplementedError

"""Research plane (Phase 2): research pipeline, evidence, claims, verification.

The engine is provider-neutral: it composes the ResearchProvider,
SourceProvider, EvidenceExtractor, and ClaimVerifier contracts from
``factory.providers.research``. Everything runs as a resumable RESEARCH job
producing versioned artifacts (research_plan → research_report).

Originality boundary: this plane produces independently researched evidence.
Competitor videos are market/context evidence only — the research never
rewrites or reproduces competitor content.
"""

from factory.research.engine import ResearchEngine
from factory.research.pipeline import ResearchPipeline, build_default_research_pipeline

__all__ = [
    "ResearchEngine",
    "ResearchPipeline",
    "build_default_research_pipeline",
]

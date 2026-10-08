"""Provider-neutral research-plane types and contracts.

Normalized research models live here (not in any vendor package) so the
research engine never depends on a specific search engine, website, LLM, or
retrieval implementation — the same boundary discipline as the discovery and
LLM planes.
"""

from factory.providers.research.types import (
    ClaimDraft,
    CollectedSource,
    ContradictionFinding,
    SourceCandidate,
    VerificationResult,
)

__all__ = [
    "ClaimDraft",
    "CollectedSource",
    "ContradictionFinding",
    "SourceCandidate",
    "VerificationResult",
]

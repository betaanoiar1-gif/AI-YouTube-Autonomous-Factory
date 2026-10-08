"""Intelligence plane (Phase 1): discovery, analysis, clustering, opportunities.

The engines in this package are deterministic and provider-independent: they
operate on normalized artifacts and provider-neutral models. No LLM dependency
exists in this plane by design (see ADR-0011) — a future embedding/LLM provider
can enhance the clustering layer through the :class:`Clusterer` protocol
without changing callers.
"""

from factory.intelligence.analysis import AnalysisEngine
from factory.intelligence.clustering import Clusterer, ClusteringResult, DeterministicClusterer
from factory.intelligence.discovery import DiscoveryEngine
from factory.intelligence.opportunities import OpportunityEngine
from factory.intelligence.pipeline import IntelligencePipeline, build_default_pipeline

__all__ = [
    "AnalysisEngine",
    "Clusterer",
    "ClusteringResult",
    "DeterministicClusterer",
    "DiscoveryEngine",
    "IntelligencePipeline",
    "OpportunityEngine",
    "build_default_pipeline",
]

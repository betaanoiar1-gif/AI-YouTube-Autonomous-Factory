"""Research pipeline: wires the research engine to the job system.

The RESEARCH job handler loads the opportunity_list artifact (never re-runs
Phase 1), runs the checkpointed research engine, and completes with the
research_report artifact reference.
"""

from __future__ import annotations

from typing import Any

from factory.config.research_config import ResearchSettings, get_research_settings
from factory.jobs.runner import JobContext, run_job
from factory.jobs.service import JobService
from factory.jobs.types import JobRecord, JobType
from factory.providers.research.base import (
    ClaimVerifier,
    EvidenceExtractor,
    ResearchProvider,
    SourceProvider,
)
from factory.research.engine import ResearchEngine
from factory.research.extractor import DeterministicEvidenceExtractor
from factory.research.planner import DeterministicResearchPlanner
from factory.research.verifier import DeterministicClaimVerifier
from factory.security.fetch import SafeFetcher
from factory.storage.artifacts import ArtifactStore


class ResearchPipeline:
    """The Phase 2 research pipeline (RESEARCH job)."""

    def __init__(
        self,
        *,
        engine: ResearchEngine,
    ) -> None:
        self._engine = engine

    def research_handler(self, context: JobContext) -> str | None:
        """RESEARCH: opportunity_list artifact → research_report artifact."""
        return self._engine.run(context.job, context)

    @property
    def handlers(self) -> dict[JobType, Any]:
        return {JobType.RESEARCH: self.research_handler}

    def run_job(self, service: JobService, job_id: str) -> JobRecord:
        """Run a research job through the existing job runner."""
        job = service.get(job_id)
        handler = self.handlers.get(job.type)
        if handler is None:
            raise ValueError(f"no research handler registered for job type {job.type.value}")
        return run_job(service, job_id, handler)


def build_default_research_pipeline(
    *,
    session_factory: Any,
    artifact_store: ArtifactStore,
    research_settings: ResearchSettings | None = None,
    source_provider: SourceProvider | None = None,
    planner: ResearchProvider | None = None,
    extractor: EvidenceExtractor | None = None,
    verifier: ClaimVerifier | None = None,
) -> ResearchPipeline:
    """Build the production research pipeline (DB-backed source cache)."""
    from factory.providers.research.web import WebSourceProvider
    from factory.research.sources import SQLiteSourceCache

    settings = research_settings or get_research_settings()
    cache = SQLiteSourceCache(
        session_factory,
        ttl_seconds=settings.source_cache_ttl_seconds,
        max_content_bytes=settings.max_cached_content_bytes,
    )
    provider = source_provider or WebSourceProvider(
        settings=settings,
        fetcher=SafeFetcher(
            timeout_seconds=settings.fetch_timeout_seconds,
            max_bytes=settings.max_collection_bytes,
            max_redirects=settings.max_redirects,
        ),
        source_cache=cache,
    )
    engine = ResearchEngine(
        planner=planner or DeterministicResearchPlanner(),
        source_provider=provider,
        extractor=extractor or DeterministicEvidenceExtractor(settings=settings),
        verifier=verifier or DeterministicClaimVerifier(),
        artifact_store=artifact_store,
        source_cache=cache,
        settings=settings,
    )
    return ResearchPipeline(engine=engine)

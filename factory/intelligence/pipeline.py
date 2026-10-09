"""Intelligence pipeline: wires the Phase 1 engines to the job system.

Each engine exposes a job handler matching the
:class:`~factory.jobs.runner.JobHandler` protocol. Handlers:

* read their input artifact (analysis/opportunity stages never re-run
  discovery — they load the existing artifact, so a failed analysis retries
  against the same discovery artifact);
* produce the next versioned artifact through the artifact store;
* complete the job with the output artifact reference.

The pipeline is resumable end to end through the existing job system
(state machine, retries, checkpoints, idempotency, durable events, usage
tracking).
"""

from __future__ import annotations

from typing import Any

from factory.config.youtube_config import YouTubeSettings, get_youtube_settings
from factory.intelligence.analysis import AnalysisEngine
from factory.intelligence.discovery import DiscoveryEngine
from factory.intelligence.opportunities import OpportunityEngine
from factory.jobs.runner import JobContext, run_job
from factory.jobs.service import JobService
from factory.jobs.types import JobRecord, JobType
from factory.providers.base import DiscoveryProvider
from factory.schemas.artifacts import (
    AnalysisResult,
    ArtifactType,
    DiscoveryResult,
    validate_artifact_payload,
)
from factory.storage.artifacts import ArtifactStore


class IntelligencePipeline:
    """The Phase 1 intelligence pipeline (discovery → analysis → opportunities)."""

    def __init__(
        self,
        *,
        provider: DiscoveryProvider,
        artifact_store: ArtifactStore,
        youtube_settings: YouTubeSettings,
        analysis_engine: AnalysisEngine | None = None,
        opportunity_engine: OpportunityEngine | None = None,
    ) -> None:
        self._provider = provider
        self._artifact_store = artifact_store
        self._discovery = DiscoveryEngine(provider, artifact_store, settings=youtube_settings)
        self._analysis = analysis_engine or AnalysisEngine()
        self._opportunities = opportunity_engine or OpportunityEngine()

    # ------------------------------------------------------------------
    # Job handlers (JobHandler protocol)
    # ------------------------------------------------------------------

    def discovery_handler(self, context: JobContext) -> str | None:
        """YOUTUBE_DISCOVERY: params → discovery_result artifact."""
        return self._discovery.run(context.job, context)

    def analysis_handler(self, context: JobContext) -> str | None:
        """MARKET_ANALYSIS: discovery_result artifact → analysis_result artifact."""
        job = context.job
        discovery_payload, source_id = self._load_input(
            job,
            artifact_type=ArtifactType.DISCOVERY_RESULT,
            payload_keys=("discovery_artifact_id", "input_artifact_id"),
        )
        discovery = DiscoveryResult.model_validate(discovery_payload)
        payload = self._analysis.analyze(
            discovery,
            project_id=job.project_id or discovery.project_id,
            analysis_id=job.id,
            source_artifact_id=source_id,
        )
        record = self._artifact_store.save(
            ArtifactType.ANALYSIS_RESULT,
            job.project_id or discovery.project_id,
            payload,
            job_id=job.id,
            metadata={"job_type": job.type.value, "source_artifact_id": source_id},
        )
        return record.id

    def opportunity_handler(self, context: JobContext) -> str | None:
        """OPPORTUNITY_DETECTION: analysis_result artifact → opportunity_list."""
        job = context.job
        analysis_payload, source_id = self._load_input(
            job,
            artifact_type=ArtifactType.ANALYSIS_RESULT,
            payload_keys=("analysis_artifact_id", "input_artifact_id"),
        )
        analysis = AnalysisResult.model_validate(analysis_payload)
        payload = self._opportunities.generate(
            analysis,
            project_id=job.project_id or analysis.project_id,
            opportunity_list_id=job.id,
            source_artifact_id=source_id,
        )
        record = self._artifact_store.save(
            ArtifactType.OPPORTUNITY_LIST,
            job.project_id or analysis.project_id,
            payload,
            job_id=job.id,
            metadata={"job_type": job.type.value, "source_artifact_id": source_id},
        )
        return record.id

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    @property
    def handlers(self) -> dict[JobType, Any]:
        """Job type → handler mapping."""
        return {
            JobType.YOUTUBE_DISCOVERY: self.discovery_handler,
            JobType.MARKET_ANALYSIS: self.analysis_handler,
            JobType.OPPORTUNITY_DETECTION: self.opportunity_handler,
        }

    def run_job(self, service: JobService, job_id: str) -> JobRecord:
        """Run a job through the existing job runner with this pipeline's handler."""
        job = service.get(job_id)
        handler = self.handlers.get(job.type)
        if handler is None:
            raise ValueError(f"no intelligence handler registered for job type {job.type.value}")
        return run_job(service, job_id, handler)

    def _load_input(
        self,
        job: JobRecord,
        *,
        artifact_type: ArtifactType,
        payload_keys: tuple[str, ...],
    ) -> tuple[dict[str, Any], str | None]:
        """Load a handler's input artifact; returns (payload, artifact_id).

        Resolution order: explicit payload key → job.input_artifact_id → the
        latest artifact of that type for the project. Never re-runs the
        producing stage.
        """
        artifact_id = None
        for key in payload_keys:
            value = (job.payload or {}).get(key)
            if isinstance(value, str) and value:
                artifact_id = value
                break
        if artifact_id is None:
            artifact_id = job.input_artifact_id
        if artifact_id:
            _record, payload = self._artifact_store.load(artifact_id)
            return validate_artifact_payload(artifact_type, payload), artifact_id
        if not job.project_id:
            raise ValueError(
                f"job {job.id} has no input artifact reference and no project to search"
            )
        record, payload = self._artifact_store.load_latest(job.project_id, artifact_type)
        return validate_artifact_payload(artifact_type, payload), record.id


def build_default_pipeline(
    *,
    session_factory: Any,
    artifact_store: ArtifactStore,
    youtube_settings: YouTubeSettings | None = None,
) -> IntelligencePipeline:
    """Build the production pipeline: YouTube provider wired to DB cache + usage."""
    from factory.providers.youtube.provider import YouTubeDiscoveryProvider

    settings = youtube_settings or get_youtube_settings()
    provider = YouTubeDiscoveryProvider.with_sqlite_cache(settings, session_factory)
    return IntelligencePipeline(
        provider=provider,
        artifact_store=artifact_store,
        youtube_settings=settings,
    )

"""Phase 3A content pipeline wiring."""

from __future__ import annotations

from typing import Any

from factory.content.engine import ContentEngine
from factory.jobs.runner import JobContext, run_job
from factory.jobs.service import JobService
from factory.jobs.types import JobRecord, JobType
from factory.storage.artifacts import ArtifactStore


class ContentPipeline:
    """Resumable content brief → narrative outline job."""

    def __init__(self, *, engine: ContentEngine) -> None:
        self._engine = engine

    def content_handler(self, context: JobContext) -> str:
        return self._engine.run(context.job, context)

    @property
    def handlers(self) -> dict[JobType, Any]:
        return {JobType.CONTENT_BRIEF: self.content_handler}

    def run_job(self, service: JobService, job_id: str) -> JobRecord:
        job = service.get(job_id)
        handler = self.handlers.get(job.type)
        if handler is None:
            raise ValueError(f"no content handler registered for job type {job.type.value}")
        return run_job(service, job_id, handler)


def build_default_content_pipeline(*, artifact_store: ArtifactStore) -> ContentPipeline:
    """Build the deterministic offline Phase 3A pipeline; no provider credentials needed."""
    return ContentPipeline(engine=ContentEngine(artifact_store=artifact_store))

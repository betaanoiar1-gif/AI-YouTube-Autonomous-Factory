"""Job types, statuses, and the job record contract."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobType(StrEnum):
    """All pipeline job types.

    The product brief's typed jobs (DISCOVERY_JOB, ANALYSIS_JOB, RESEARCH_JOB,
    SCRIPT_JOB, PRODUCTION_JOB, QA_JOB) map onto these discriminators; extra
    types cover the remaining pipeline stages. Typed jobs are rows in the
    single ``pipeline_jobs`` table — one resumable job system instead of N
    parallel ones.
    """

    YOUTUBE_DISCOVERY = "youtube_discovery"  # DISCOVERY_JOB
    MARKET_ANALYSIS = "market_analysis"  # ANALYSIS_JOB
    OPPORTUNITY_DETECTION = "opportunity_detection"
    RESEARCH = "research"  # RESEARCH_JOB
    CONTENT_BRIEF = "content_brief"
    SCRIPT = "script"  # SCRIPT_JOB
    VISUAL_PLAN = "visual_plan"
    ASSET_ACQUISITION = "asset_acquisition"
    VOICE_GENERATION = "voice_generation"
    RENDER = "render"  # RenderJob / PRODUCTION_JOB
    QA = "qa"  # QA_JOB
    PUBLISH_PACKAGE = "publish_package"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Allowed status transitions. FAILED/CANCELLED -> PENDING is the restart path.
ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: {JobStatus.PENDING},
    JobStatus.CANCELLED: {JobStatus.PENDING},
}

TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobErrorInfo(BaseModel):
    """Sanitized error information for a failed job attempt."""

    model_config = ConfigDict(extra="forbid")

    type: str
    message: str
    retryable: bool = True
    at: datetime = Field(default_factory=_utcnow)


class JobRecord(BaseModel):
    """The job contract: id, type, status, timestamps, progress, error info,
    retry info, and input/output artifact references."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: JobType
    status: JobStatus = JobStatus.PENDING
    project_id: str | None = None
    idempotency_key: str | None = None
    progress: int = Field(default=0, ge=0, le=100)
    attempts: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    input_artifact_id: str | None = None
    output_artifact_id: str | None = None
    error: JobErrorInfo | None = None
    next_retry_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def discovery(cls, project_id: str, **kwargs: Any) -> JobRecord:
        """Convenience constructor for a DISCOVERY_JOB record."""
        return cls(type=JobType.YOUTUBE_DISCOVERY, project_id=project_id, **kwargs)

    @classmethod
    def render(cls, project_id: str, **kwargs: Any) -> JobRecord:
        """Convenience constructor for a RenderJob (RENDER / PRODUCTION_JOB)."""
        return cls(type=JobType.RENDER, project_id=project_id, **kwargs)

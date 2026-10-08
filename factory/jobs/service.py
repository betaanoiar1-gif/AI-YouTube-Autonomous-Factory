"""JobService: enqueue, claim, progress, checkpoints, completion, failure,
cancellation, and restart — with a validated state machine and retry policy.

Semantics (see docs/job-system.md):

* **Resumable** — handlers persist checkpoints through the service; a
  restarted job reloads its checkpoint and continues without corrupting
  completed work.
* **Retryable** — a failed attempt schedules a retry (back to PENDING with
  ``next_retry_at``) until ``max_retries`` is exhausted, then the job FAILS.
* **Restartable** — FAILED and CANCELLED jobs can be restarted (back to
  PENDING) with attempts preserved and error info cleared.
* **Idempotent enqueue** — an optional idempotency key returns the existing
  active job instead of creating a duplicate.
* **Observable** — lifecycle transitions are recorded as ``job_events`` rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from factory.errors import (
    InvalidJobTransitionError,
    JobError,
    JobNotFoundError,
)
from factory.jobs.types import (
    ALLOWED_TRANSITIONS,
    JobErrorInfo,
    JobRecord,
    JobStatus,
    JobType,
)
from factory.observability.logging import get_logger
from factory.storage.models import JobEvent, PipelineJob

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobService:
    """Database-backed job service."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 3600,
    ) -> None:
        self._session_factory = session_factory
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        job_type: JobType | str,
        *,
        project_id: str | None = None,
        payload: dict[str, Any] | None = None,
        max_retries: int = 3,
        idempotency_key: str | None = None,
        input_artifact_id: str | None = None,
    ) -> JobRecord:
        """Create a PENDING job (or return the existing one for an idempotency key)."""
        job_type = JobType(job_type)
        with self._session_factory() as session:
            if idempotency_key is not None:
                existing = session.execute(
                    select(PipelineJob).where(PipelineJob.idempotency_key == idempotency_key)
                ).scalar_one_or_none()
                if existing is not None and JobStatus(existing.status) not in (
                    JobStatus.SUCCEEDED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                ):
                    return self._to_record(existing)
            row = PipelineJob(
                id=uuid.uuid4().hex,
                type=job_type.value,
                status=JobStatus.PENDING.value,
                project_id=project_id,
                idempotency_key=idempotency_key,
                payload=payload or {},
                max_retries=max_retries,
                input_artifact_id=input_artifact_id,
            )
            session.add(row)
            session.commit()
            record = self._to_record(row)
        self._emit(record.id, "INFO", "job_enqueued", {"job_type": job_type.value})
        logger.info(
            "job_enqueued",
            extra={"job_id": record.id, "job_type": job_type.value, "project_id": project_id},
        )
        return record

    # ------------------------------------------------------------------
    # Claim / progress / checkpoints
    # ------------------------------------------------------------------

    def claim(self, job_id: str) -> JobRecord | None:
        """Atomically move a PENDING job to RUNNING (single-claim semantics)."""
        with self._session_factory() as session:
            row = session.get(PipelineJob, job_id)
            if row is None:
                raise JobNotFoundError(f"Job not found: {job_id}")
            if row.status != JobStatus.PENDING.value:
                return None
            row.status = JobStatus.RUNNING.value
            row.attempts += 1
            row.started_at = _utcnow()
            row.updated_at = _utcnow()
            session.commit()
            record = self._to_record(row)
        self._emit(job_id, "INFO", "job_started", {"attempt": record.attempts})
        return record

    def update_progress(
        self, job_id: str, progress: int, *, checkpoint: dict[str, Any] | None = None
    ) -> JobRecord:
        """Update progress (0-100) and optionally merge a checkpoint."""
        if not 0 <= progress <= 100:
            raise JobError(f"Progress must be within 0-100, got {progress}")
        with self._session_factory() as session:
            row = self._get_row(session, job_id)
            row.progress = progress
            if checkpoint is not None:
                merged = dict(row.checkpoint or {})
                merged.update(checkpoint)
                row.checkpoint = merged
            row.updated_at = _utcnow()
            session.commit()
            return self._to_record(row)

    def save_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> JobRecord:
        """Merge and persist a checkpoint (the resumability mechanism)."""
        return self.update_progress(job_id, self.get(job_id).progress, checkpoint=checkpoint)

    def load_checkpoint(self, job_id: str) -> dict[str, Any]:
        """Load the persisted checkpoint for a job ({} when none)."""
        return self.get(job_id).checkpoint

    # ------------------------------------------------------------------
    # Completion / failure / cancellation / restart
    # ------------------------------------------------------------------

    def complete(self, job_id: str, *, output_artifact_id: str | None = None) -> JobRecord:
        """Mark a RUNNING job SUCCEEDED."""
        with self._session_factory() as session:
            row = self._get_row(session, job_id)
            self._transition(row, JobStatus.SUCCEEDED)
            row.progress = 100
            row.output_artifact_id = output_artifact_id
            row.finished_at = _utcnow()
            row.updated_at = _utcnow()
            session.commit()
            record = self._to_record(row)
        self._emit(job_id, "INFO", "job_succeeded", {"output_artifact_id": output_artifact_id})
        logger.info("job_succeeded", extra={"job_id": job_id})
        return record

    def fail(
        self,
        job_id: str,
        error: JobErrorInfo | Exception,
        *,
        retryable: bool = True,
    ) -> JobRecord:
        """Record a failed attempt.

        If attempts remain and the error is retryable, the job goes back to
        PENDING with ``next_retry_at`` set (exponential backoff); otherwise it
        becomes FAILED.
        """
        if isinstance(error, JobErrorInfo):
            error_info = error
        else:
            error_info = JobErrorInfo(
                type=type(error).__name__,
                message=str(error),
                retryable=retryable,
            )
        with self._session_factory() as session:
            row = self._get_row(session, job_id)
            row.error_type = error_info.type
            row.error_message = error_info.message
            row.updated_at = _utcnow()
            if retryable and row.attempts <= row.max_retries:
                delay = min(
                    self._retry_base_seconds * (2 ** max(row.attempts - 1, 0)),
                    self._retry_max_seconds,
                )
                row.status = JobStatus.PENDING.value
                row.next_retry_at = _utcnow() + timedelta(seconds=delay)
                session.commit()
                record = self._to_record(row)
                self._emit(
                    job_id,
                    "WARNING",
                    "job_retry_scheduled",
                    {
                        "attempt": row.attempts,
                        "delay_seconds": delay,
                        "error_type": error_info.type,
                    },
                )
                logger.warning(
                    "job_retry_scheduled",
                    extra={"job_id": job_id, "attempt": row.attempts, "delay_seconds": delay},
                )
                return record
            self._transition(row, JobStatus.FAILED)
            row.finished_at = _utcnow()
            session.commit()
            record = self._to_record(row)
        self._emit(job_id, "ERROR", "job_failed", {"error_type": error_info.type})
        logger.error(
            "job_failed",
            extra={"job_id": job_id, "error_type": error_info.type},
        )
        return record

    def cancel(self, job_id: str) -> JobRecord:
        """Cancel a PENDING or RUNNING job."""
        with self._session_factory() as session:
            row = self._get_row(session, job_id)
            self._transition(row, JobStatus.CANCELLED)
            row.finished_at = _utcnow()
            row.updated_at = _utcnow()
            session.commit()
            record = self._to_record(row)
        self._emit(job_id, "INFO", "job_cancelled", {})
        logger.info("job_cancelled", extra={"job_id": job_id})
        return record

    def restart(self, job_id: str) -> JobRecord:
        """Restart a FAILED or CANCELLED job (attempts preserved, error cleared)."""
        with self._session_factory() as session:
            row = self._get_row(session, job_id)
            self._transition(row, JobStatus.PENDING)
            row.error_type = None
            row.error_message = None
            row.next_retry_at = None
            row.finished_at = None
            row.updated_at = _utcnow()
            session.commit()
            record = self._to_record(row)
        self._emit(job_id, "INFO", "job_restarted", {"attempts": record.attempts})
        logger.info("job_restarted", extra={"job_id": job_id, "attempts": record.attempts})
        return record

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord:
        with self._session_factory() as session:
            return self._to_record(self._get_row(session, job_id))

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        status: JobStatus | str | None = None,
        job_type: JobType | str | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        with self._session_factory() as session:
            stmt = select(PipelineJob).order_by(PipelineJob.created_at.desc()).limit(limit)
            if project_id is not None:
                stmt = stmt.where(PipelineJob.project_id == project_id)
            if status is not None:
                stmt = stmt.where(PipelineJob.status == JobStatus(status).value)
            if job_type is not None:
                stmt = stmt.where(PipelineJob.type == JobType(job_type).value)
            rows = session.execute(stmt).scalars().all()
            return [self._to_record(row) for row in rows]

    def due_for_retry(self, *, now: datetime | None = None) -> list[JobRecord]:
        """PENDING jobs whose scheduled retry time has arrived."""
        now = now or _utcnow()
        with self._session_factory() as session:
            stmt = select(PipelineJob).where(
                PipelineJob.status == JobStatus.PENDING.value,
                PipelineJob.next_retry_at.is_not(None),
                PipelineJob.next_retry_at <= now,
            )
            rows = session.execute(stmt).scalars().all()
            return [self._to_record(row) for row in rows]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def add_event(
        self, job_id: str, level: str, message: str, context: dict[str, Any] | None = None
    ) -> None:
        """Append a durable structured event to a job's log."""
        self._emit(job_id, level, message, context or {})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _emit(self, job_id: str, level: str, message: str, context: dict[str, Any]) -> None:
        try:
            with self._session_factory() as session:
                session.add(
                    JobEvent(job_id=job_id, level=level.upper(), message=message, context=context)
                )
                session.commit()
        except Exception:
            logger.exception("job_event_write_failed", extra={"job_id": job_id})

    @staticmethod
    def _get_row(session: Session, job_id: str) -> PipelineJob:
        row = session.get(PipelineJob, job_id)
        if row is None:
            raise JobNotFoundError(f"Job not found: {job_id}")
        return row

    @staticmethod
    def _transition(row: PipelineJob, target: JobStatus) -> None:
        current = JobStatus(row.status)
        if target not in ALLOWED_TRANSITIONS[current]:
            raise InvalidJobTransitionError(row.id, current.value, target.value)
        row.status = target.value

    @staticmethod
    def _to_record(row: PipelineJob) -> JobRecord:
        error = None
        if row.error_type or row.error_message:
            error = JobErrorInfo(
                type=row.error_type or "UnknownError",
                message=row.error_message or "",
            )
        return JobRecord(
            id=row.id,
            type=JobType(row.type),
            status=JobStatus(row.status),
            project_id=row.project_id,
            idempotency_key=row.idempotency_key,
            progress=row.progress,
            attempts=row.attempts,
            max_retries=row.max_retries,
            payload=row.payload or {},
            checkpoint=row.checkpoint or {},
            input_artifact_id=row.input_artifact_id,
            output_artifact_id=row.output_artifact_id,
            error=error,
            next_retry_at=row.next_retry_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

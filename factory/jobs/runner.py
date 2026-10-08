"""In-process job runner.

Executes a job handler against the JobService with full lifecycle handling:
claim -> run (with checkpointing) -> complete / fail(with retry) / cancel.

A later phase can move this to a dedicated worker process without changing
handlers: the contract is ``handler(JobContext) -> output_artifact_id | None``
plus checkpoint persistence for resumability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from factory.errors import JobCancelledError
from factory.jobs.service import JobService
from factory.jobs.types import JobRecord, JobStatus
from factory.observability.logging import bind_context, clear_context, get_logger

logger = get_logger(__name__)


@dataclass
class JobContext:
    """What a job handler receives: the job, its checkpoint, and controls."""

    job: JobRecord
    service: JobService
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """Merge and persist a checkpoint (survives restarts)."""
        merged = {**self.checkpoint, **checkpoint}
        self.checkpoint = merged
        self.job = self.service.save_checkpoint(self.job.id, checkpoint)

    def set_progress(self, progress: int) -> None:
        """Report progress (0-100)."""
        self.job = self.service.update_progress(self.job.id, progress)

    @property
    def job_id(self) -> str:
        return self.job.id


#: A handler returns an optional output artifact id.
JobHandler = Callable[[JobContext], "str | None"]


def run_job(service: JobService, job_id: str, handler: JobHandler) -> JobRecord:
    """Run a job to completion (or failure) and return the final record."""
    record = service.claim(job_id)
    if record is None:
        # Not claimable (already running, finished, or missing).
        return service.get(job_id)
    token = bind_context(job_id=record.id, job_type=record.type.value)
    context = JobContext(job=record, service=service, checkpoint=dict(record.checkpoint))
    try:
        output_artifact_id = handler(context)
        # A cancel requested mid-run wins over completion.
        current = service.get(job_id)
        if current.status == JobStatus.CANCELLED:
            return current
        return service.complete(job_id, output_artifact_id=output_artifact_id)
    except JobCancelledError:
        return service.cancel(job_id)
    except Exception as exc:
        logger.exception("job_handler_failed", extra={"job_id": job_id})
        return service.fail(job_id, exc)
    finally:
        clear_context(token)

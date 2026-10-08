"""Job lifecycle tests (required): pending/running/succeeded/failed/cancelled,
retries, restarts, checkpoints (resumability), idempotent enqueue, runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from factory.errors import InvalidJobTransitionError, JobError, JobNotFoundError
from factory.jobs.runner import JobContext, run_job
from factory.jobs.service import JobService
from factory.jobs.types import JobStatus, JobType
from factory.storage.models import JobEvent


class TestLifecycle:
    def test_enqueue_creates_pending_job(self, job_service: JobService):
        job = job_service.enqueue(JobType.DISCOVERY, project_id="proj-1", payload={"q": "x"})
        assert job.status == JobStatus.PENDING
        assert job.type == JobType.DISCOVERY
        assert job.progress == 0
        assert job.attempts == 0
        assert job.payload == {"q": "x"}

    def test_claim_moves_to_running_and_counts_attempt(self, job_service: JobService):
        job = job_service.enqueue(JobType.ANALYSIS)
        claimed = job_service.claim(job.id)
        assert claimed is not None
        assert claimed.status == JobStatus.RUNNING
        assert claimed.attempts == 1
        assert claimed.started_at is not None
        # Second claim returns None (already running).
        assert job_service.claim(job.id) is None

    def test_claim_missing_job_raises(self, job_service: JobService):
        with pytest.raises(JobNotFoundError):
            job_service.claim("does-not-exist")

    def test_progress_and_checkpoint(self, job_service: JobService):
        job = job_service.enqueue(JobType.RESEARCH)
        job_service.claim(job.id)
        job_service.update_progress(job.id, 40)
        job_service.save_checkpoint(job.id, {"stage": "collect", "done": ["a"]})
        job_service.save_checkpoint(job.id, {"done": ["a", "b"]})
        current = job_service.get(job.id)
        assert current.progress == 40
        assert current.checkpoint == {"stage": "collect", "done": ["a", "b"]}

    def test_progress_out_of_range_rejected(self, job_service: JobService):
        job = job_service.enqueue(JobType.RESEARCH)
        with pytest.raises(JobError):
            job_service.update_progress(job.id, 101)

    def test_complete(self, job_service: JobService):
        job = job_service.enqueue(JobType.SCRIPT)
        job_service.claim(job.id)
        done = job_service.complete(job.id, output_artifact_id="artifact-1")
        assert done.status == JobStatus.SUCCEEDED
        assert done.progress == 100
        assert done.output_artifact_id == "artifact-1"
        assert done.finished_at is not None

    def test_invalid_transition_rejected(self, job_service: JobService):
        job = job_service.enqueue(JobType.SCRIPT)
        # PENDING -> SUCCEEDED is not allowed.
        with pytest.raises(InvalidJobTransitionError):
            job_service.complete(job.id)

    def test_cancel_pending_and_running(self, job_service: JobService):
        job = job_service.enqueue(JobType.QA)
        cancelled = job_service.cancel(job.id)
        assert cancelled.status == JobStatus.CANCELLED
        assert cancelled.finished_at is not None

        job2 = job_service.enqueue(JobType.QA)
        job_service.claim(job2.id)
        assert job_service.cancel(job2.id).status == JobStatus.CANCELLED

    def test_succeeded_is_terminal(self, job_service: JobService):
        job = job_service.enqueue(JobType.QA)
        job_service.claim(job.id)
        job_service.complete(job.id)
        with pytest.raises(InvalidJobTransitionError):
            job_service.cancel(job.id)
        with pytest.raises(InvalidJobTransitionError):
            job_service.restart(job.id)

    def test_idempotent_enqueue(self, job_service: JobService):
        first = job_service.enqueue(JobType.DISCOVERY, idempotency_key="run-1")
        second = job_service.enqueue(JobType.DISCOVERY, idempotency_key="run-1")
        assert first.id == second.id


class TestRetryAndRestart:
    def test_retryable_failure_schedules_retry(self, job_service: JobService):
        job = job_service.enqueue(JobType.RESEARCH, max_retries=3)
        job_service.claim(job.id)
        failed = job_service.fail(job.id, ValueError("transient"), retryable=True)
        assert failed.status == JobStatus.PENDING  # back to pending for retry
        assert failed.next_retry_at is not None
        assert failed.error is not None
        assert failed.error.type == "ValueError"
        assert failed.attempts == 1

    def test_retries_exhausted_marks_failed(self, job_service: JobService):
        job = job_service.enqueue(JobType.RESEARCH, max_retries=1)
        job_service.claim(job.id)  # attempt 1
        job_service.fail(job.id, ValueError("boom"), retryable=True)  # retry scheduled
        job_service.claim(job.id)  # attempt 2
        failed = job_service.fail(job.id, ValueError("boom again"), retryable=True)
        assert failed.status == JobStatus.FAILED
        assert failed.finished_at is not None

    def test_non_retryable_failure_fails_immediately(self, job_service: JobService):
        job = job_service.enqueue(JobType.RESEARCH, max_retries=3)
        job_service.claim(job.id)
        failed = job_service.fail(job.id, ValueError("permanent"), retryable=False)
        assert failed.status == JobStatus.FAILED

    def test_restart_failed_job_preserves_attempts_and_clears_error(self, job_service: JobService):
        job = job_service.enqueue(JobType.RENDER, max_retries=0)
        job_service.claim(job.id)
        job_service.fail(job.id, ValueError("boom"), retryable=False)
        assert job_service.get(job.id).status == JobStatus.FAILED

        restarted = job_service.restart(job.id)
        assert restarted.status == JobStatus.PENDING
        assert restarted.attempts == 1  # preserved
        assert restarted.error is None
        assert restarted.next_retry_at is None

    def test_restart_cancelled_job(self, job_service: JobService):
        job = job_service.enqueue(JobType.RENDER)
        job_service.cancel(job.id)
        restarted = job_service.restart(job.id)
        assert restarted.status == JobStatus.PENDING

    def test_due_for_retry(self, job_service: JobService):
        job = job_service.enqueue(JobType.RESEARCH, max_retries=3)
        job_service.claim(job.id)
        job_service.fail(job.id, ValueError("boom"))
        due = job_service.due_for_retry(now=datetime.now(UTC) + timedelta(hours=1))
        assert [j.id for j in due] == [job.id]
        not_due = job_service.due_for_retry(now=datetime.now(UTC))
        assert not_due == []


class TestRunner:
    def test_runner_success(self, job_service: JobService):
        job = job_service.enqueue(JobType.SCRIPT, project_id="p1")

        checkpoints_at_entry: list[dict] = []

        def handler(context: JobContext) -> str | None:
            checkpoints_at_entry.append(dict(context.checkpoint))
            context.set_progress(50)
            context.save_checkpoint({"half": True})
            return "artifact-out"

        record = run_job(job_service, job.id, handler)
        assert record.status == JobStatus.SUCCEEDED
        assert record.output_artifact_id == "artifact-out"
        assert checkpoints_at_entry == [{}]  # fresh job starts with no checkpoint

    def test_runner_failure_and_resume_via_checkpoint(self, job_service: JobService):
        """A failed job restarts without corrupting completed work."""
        job = job_service.enqueue(JobType.RESEARCH, max_retries=2)
        attempts: list[dict] = []

        def handler(context: JobContext) -> str | None:
            attempts.append(dict(context.checkpoint))
            done = list(context.checkpoint.get("done", []))
            if len(attempts) == 1:
                done.append("step-1")
                context.save_checkpoint({"done": done})
                raise RuntimeError("transient provider outage")
            done.append("step-2")
            context.save_checkpoint({"done": done})
            return None

        first = run_job(job_service, job.id, handler)
        assert first.status == JobStatus.PENDING  # retry scheduled
        assert first.checkpoint == {"done": ["step-1"]}

        second = run_job(job_service, job.id, handler)
        assert second.status == JobStatus.SUCCEEDED
        assert attempts[1] == {"done": ["step-1"]}  # resumed from checkpoint
        assert second.checkpoint == {"done": ["step-1", "step-2"]}

    def test_runner_exhaustion_marks_failed(self, job_service: JobService):
        job = job_service.enqueue(JobType.RESEARCH, max_retries=0)

        def handler(context: JobContext) -> str | None:
            raise RuntimeError("always fails")

        record = run_job(job_service, job.id, handler)
        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert record.error.type == "RuntimeError"

    def test_runner_cancellation(self, job_service: JobService):
        from factory.errors import JobCancelledError

        job = job_service.enqueue(JobType.RENDER)

        def handler(context: JobContext) -> str | None:
            raise JobCancelledError("stop requested")

        record = run_job(job_service, job.id, handler)
        assert record.status == JobStatus.CANCELLED

    def test_runner_unclaimable_returns_current(self, job_service: JobService):
        job = job_service.enqueue(JobType.QA)
        job_service.claim(job.id)
        job_service.complete(job.id)
        record = run_job(job_service, job.id, lambda ctx: None)
        assert record.status == JobStatus.SUCCEEDED

    def test_job_events_recorded(self, job_service: JobService, session_factory):
        from sqlalchemy import select

        job = job_service.enqueue(JobType.QA)
        run_job(job_service, job.id, lambda ctx: None)
        with session_factory() as session:
            events = (
                session.execute(
                    select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.created_at)
                )
                .scalars()
                .all()
            )
        messages = [event.message for event in events]
        assert "job_enqueued" in messages
        assert "job_started" in messages
        assert "job_succeeded" in messages

    def test_list_filters(self, job_service: JobService):
        job_service.enqueue(JobType.DISCOVERY, project_id="p1")
        job_service.enqueue(JobType.QA, project_id="p2")
        assert len(job_service.list_jobs(project_id="p1")) == 1
        assert len(job_service.list_jobs(job_type=JobType.QA)) == 1
        assert len(job_service.list_jobs(status=JobStatus.PENDING)) == 2

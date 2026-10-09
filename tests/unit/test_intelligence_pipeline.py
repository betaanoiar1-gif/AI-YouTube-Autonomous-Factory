"""Intelligence pipeline unit tests (job-system integration).

Uses a stub DiscoveryProvider that implements the production interface
in-memory — this tests the JOB WIRING (state machine, artifacts, checkpoints,
idempotency, retries) deterministically. The production YouTube provider is
tested separately against mocked HTTP (test_youtube_provider.py) and the
local simulation (tests/simulated/).

Covers: discovery → artifact, artifact → analysis, analysis → opportunities,
checkpoint resume, idempotency, failure/retry, artifact integrity, and
"a failed analysis never re-runs discovery".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from factory.config.youtube_config import YouTubeSettings
from factory.errors import ArtifactIntegrityError
from factory.intelligence.pipeline import IntelligencePipeline
from factory.jobs.service import JobService
from factory.jobs.types import JobStatus, JobType
from factory.providers.base import DiscoveryProvider
from factory.providers.discovery.types import (
    DiscoveredChannelItem,
    DiscoveredVideoItem,
    DiscoveryPage,
    DiscoveryQuery,
)
from factory.schemas.artifacts import (
    AnalysisResult,
    DiscoveryResult,
    OpportunityList,
)

NOW = datetime(2025, 1, 1, tzinfo=UTC)


class StubDiscoveryProvider(DiscoveryProvider):
    """In-memory DiscoveryProvider implementing the production interface."""

    name = "stub-youtube"

    def __init__(self, video_count: int = 12, channel_count: int = 3) -> None:
        self.videos = [
            DiscoveredVideoItem(
                video_id=f"vid{i}",
                channel_id=f"UC{i % channel_count}",
                title=f"The forgotten tunnels of place {i}",
                url=f"https://www.youtube.com/watch?v=vid{i}",
                published_at=NOW - timedelta(days=5 + i),
                duration_seconds=600 + i * 60,
                views=1000 * (i + 1),
                likes=10 * (i + 1),
                comments=i,
                channel_title=f"Channel {i % channel_count}",
                description_chars=100,
            )
            for i in range(video_count)
        ]
        self.channels = [
            DiscoveredChannelItem(
                channel_id=f"UC{i}",
                title=f"Channel {i}",
                subscriber_count=1000 * (i + 1),
                view_count=100000 * (i + 1),
                video_count=10,
            )
            for i in range(channel_count)
        ]
        self.search_calls = 0
        self.video_metric_calls = 0
        self.channel_metric_calls = 0

    def discover_videos(
        self,
        query: str | DiscoveryQuery | dict[str, Any],
        *,
        max_results: int = 50,
        job_id: str | None = None,
        page_token: str | None = None,
        max_pages: int | None = None,
    ) -> DiscoveryPage:
        self.search_calls += 1
        start = int(page_token.split("-")[1]) if page_token else 0
        items = self.videos[start : start + 2]  # small pages to exercise pagination
        next_token = f"page-{start + 2}" if start + 2 < len(self.videos) else None
        return DiscoveryPage(
            items=items,
            next_page_token=next_token,
            total_results=len(self.videos),
            quota_units_used=100,
        )

    def get_video_metrics(
        self, video_ids: list[str], *, job_id: str | None = None
    ) -> list[DiscoveredVideoItem]:
        self.video_metric_calls += 1
        by_id = {v.video_id: v for v in self.videos}
        return [by_id[vid] for vid in video_ids if vid in by_id]

    def get_channel_metrics(
        self, channel_ids: str | list[str], *, job_id: str | None = None
    ) -> list[DiscoveredChannelItem]:
        self.channel_metric_calls += 1
        ids = [channel_ids] if isinstance(channel_ids, str) else channel_ids
        by_id = {c.channel_id: c for c in self.channels}
        return [by_id[cid] for cid in ids if cid in by_id]

    def quota_used(self) -> int | None:
        return 100 * self.search_calls + self.video_metric_calls + self.channel_metric_calls


@pytest.fixture
def stub_provider() -> StubDiscoveryProvider:
    return StubDiscoveryProvider()


@pytest.fixture
def pipeline(stub_provider, session_factory, artifact_store) -> IntelligencePipeline:
    settings = YouTubeSettings(
        youtube_API_KEY="AIzaSyTEST_FAKE_KEY_0000000000000001",
        discovery_result_limit=50,
        search_max_results_per_page=2,
    )
    return IntelligencePipeline(
        provider=stub_provider,
        artifact_store=artifact_store,
        youtube_settings=settings,
    )


@pytest.fixture
def job_service(projects) -> JobService:
    return JobService(projects, retry_base_seconds=1, retry_max_seconds=5)


DISCOVERY_PAYLOAD = {
    "query": "forgotten tunnels",
    "language": "en",
    "target_audience": "history enthusiasts",
    "result_limit": 8,
    "page_size": 2,
}


class TestPipelineStages:
    def test_discovery_produces_valid_artifact(
        self, pipeline: IntelligencePipeline, job_service: JobService, artifact_store, stub_provider
    ) -> None:
        job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        record = pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        assert record.output_artifact_id
        assert record.progress == 100

        _rec, payload = artifact_store.load(record.output_artifact_id)
        discovery = DiscoveryResult.model_validate(payload)
        assert discovery.project_id == "proj-1"
        assert discovery.query == "forgotten tunnels"
        assert discovery.language == "en"
        assert len(discovery.videos) == 8  # result limit honored
        assert discovery.channels
        assert discovery.quota_units_used == stub_provider.quota_used()
        assert discovery.provider["name"] == "stub-youtube"

    def test_analysis_consumes_discovery_artifact(
        self, pipeline: IntelligencePipeline, job_service: JobService, artifact_store
    ) -> None:
        discovery_job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        discovery_record = pipeline.run_job(job_service, discovery_job.id)
        assert discovery_record.status == JobStatus.SUCCEEDED

        analysis_job = job_service.enqueue(
            JobType.MARKET_ANALYSIS,
            project_id="proj-1",
            input_artifact_id=discovery_record.output_artifact_id,
        )
        record = pipeline.run_job(job_service, analysis_job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        analysis = AnalysisResult.model_validate(payload)
        assert analysis.video_count == 8
        assert analysis.source_artifact_id == discovery_record.output_artifact_id
        assert analysis.videos and analysis.patterns and analysis.scoring

    def test_opportunities_consume_analysis_artifact(
        self, pipeline: IntelligencePipeline, job_service: JobService, artifact_store
    ) -> None:
        discovery_job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        discovery_record = pipeline.run_job(job_service, discovery_job.id)
        analysis_job = job_service.enqueue(
            JobType.MARKET_ANALYSIS,
            project_id="proj-1",
            input_artifact_id=discovery_record.output_artifact_id,
        )
        analysis_record = pipeline.run_job(job_service, analysis_job.id)
        opportunity_job = job_service.enqueue(
            JobType.OPPORTUNITY_DETECTION,
            project_id="proj-1",
            input_artifact_id=analysis_record.output_artifact_id,
        )
        record = pipeline.run_job(job_service, opportunity_job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        opportunity_list = OpportunityList.model_validate(payload)
        assert opportunity_list.opportunities
        assert opportunity_list.source_artifact_id == analysis_record.output_artifact_id

    def test_analysis_falls_back_to_latest_discovery_artifact(
        self, pipeline: IntelligencePipeline, job_service: JobService
    ) -> None:
        discovery_job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        pipeline.run_job(job_service, discovery_job.id)
        # No explicit input reference → latest discovery artifact for the project:
        analysis_job = job_service.enqueue(JobType.MARKET_ANALYSIS, project_id="proj-1")
        record = pipeline.run_job(job_service, analysis_job.id)
        assert record.status == JobStatus.SUCCEEDED


class TestJobSystemIntegration:
    def test_idempotent_enqueue(
        self, pipeline: IntelligencePipeline, job_service: JobService, stub_provider
    ) -> None:
        first = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY,
            project_id="proj-1",
            payload=DISCOVERY_PAYLOAD,
            idempotency_key="run-1",
        )
        second = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY,
            project_id="proj-1",
            payload=DISCOVERY_PAYLOAD,
            idempotency_key="run-1",
        )
        assert first.id == second.id
        record = pipeline.run_job(job_service, first.id)
        assert record.status == JobStatus.SUCCEEDED
        assert stub_provider.search_calls == 4  # 8 videos / page size 2

    def test_checkpoint_resume(
        self, pipeline: IntelligencePipeline, job_service: JobService, stub_provider, monkeypatch
    ) -> None:
        # Fail the provider after the first page:
        original = stub_provider.discover_videos
        calls = {"count": 0}

        def flaky(query, **kwargs):
            calls["count"] += 1
            if calls["count"] > 1:
                raise RuntimeError("simulated provider outage")
            return original(query, **kwargs)

        monkeypatch.setattr(stub_provider, "discover_videos", flaky)
        job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        failed = pipeline.run_job(job_service, job.id)
        assert failed.status == JobStatus.PENDING  # retry scheduled
        assert failed.error is not None and failed.error.type == "RuntimeError"
        assert failed.checkpoint["videos"]  # progress was checkpointed

        # Re-run the pending job → resumes from the checkpoint:
        monkeypatch.setattr(stub_provider, "discover_videos", original)
        resumed = pipeline.run_job(job_service, job.id)
        assert resumed.status == JobStatus.SUCCEEDED
        assert resumed.output_artifact_id

    def test_failed_analysis_never_reruns_discovery(
        self,
        pipeline: IntelligencePipeline,
        job_service: JobService,
        artifact_store,
        stub_provider,
        monkeypatch,
    ) -> None:
        discovery_job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        discovery_record = pipeline.run_job(job_service, discovery_job.id)
        assert discovery_record.status == JobStatus.SUCCEEDED
        search_calls = stub_provider.search_calls

        original = pipeline.analysis_handler
        attempts = {"count": 0}

        def flaky(context):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("simulated analysis outage")
            return original(context)

        monkeypatch.setattr(pipeline, "analysis_handler", flaky)
        pipeline.handlers[JobType.MARKET_ANALYSIS] = flaky

        analysis_job = job_service.enqueue(
            JobType.MARKET_ANALYSIS,
            project_id="proj-1",
            input_artifact_id=discovery_record.output_artifact_id,
        )
        failed = pipeline.run_job(job_service, analysis_job.id)
        assert failed.status == JobStatus.PENDING

        succeeded = pipeline.run_job(job_service, analysis_job.id)
        assert succeeded.status == JobStatus.SUCCEEDED
        # Discovery never re-ran:
        assert stub_provider.search_calls == search_calls

    def test_artifact_integrity(
        self, pipeline: IntelligencePipeline, job_service: JobService, artifact_store
    ) -> None:
        job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        record = pipeline.run_job(job_service, job.id)
        artifact_record, _payload = artifact_store.load(record.output_artifact_id)
        path = artifact_store.resolve_path(artifact_record.storage_ref)
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        data["query"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ArtifactIntegrityError):
            artifact_store.load(record.output_artifact_id)

    def test_artifacts_are_versioned_not_overwritten(
        self, pipeline: IntelligencePipeline, job_service: JobService, artifact_store
    ) -> None:
        first_job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        first = pipeline.run_job(job_service, first_job.id)
        second_job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        second = pipeline.run_job(job_service, second_job.id)
        assert first.output_artifact_id != second.output_artifact_id
        # Both artifacts remain readable:
        for artifact_id in (first.output_artifact_id, second.output_artifact_id):
            artifact_record, _payload = artifact_store.load(artifact_id)
            assert artifact_record.version >= 1

    def test_job_events_recorded(
        self, pipeline: IntelligencePipeline, job_service: JobService, session_factory
    ) -> None:
        from sqlalchemy import select

        from factory.storage.models import JobEvent

        job = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY, project_id="proj-1", payload=DISCOVERY_PAYLOAD
        )
        pipeline.run_job(job_service, job.id)
        with session_factory() as session:
            events = (
                session.execute(select(JobEvent).where(JobEvent.job_id == job.id)).scalars().all()
            )
        messages = [event.message for event in events]
        assert "job_enqueued" in messages
        assert "job_started" in messages
        assert "job_succeeded" in messages

"""SIMULATED / OFFLINE YouTube Data API integration tests.

These tests run the **production** intelligence pipeline against a local,
in-process HTTP simulation of the YouTube Data API v3
(``tests/simulated/youtube_server.py``):

    IntelligencePipeline (production job handlers)
      → DiscoveryProvider interface (factory.providers.base)
        → YouTubeDiscoveryProvider (production implementation)
          → YouTubeClient (production thin HTTP client, real httpx over a
            real socket to 127.0.0.1)
            → simulated YouTube Data API v3 endpoint (documented wire protocol)
              → response parsing → normalized items → versioned artifacts

No real ``youtube_API_KEY`` is required or used (the simulation accepts one
clearly-fake key), no real network egress occurs, and no real YouTube API
connectivity is claimed. The REAL YouTube API test remains an owner-run item.

Covered: the full discovery → analysis → opportunities pipeline, pagination,
deduplication, normalization, caching, quota accounting + quota-budget
enforcement, authentication failure, missing configuration, quota-exceeded,
rate limiting, 5xx retries, timeouts, malformed responses, checkpoint resume,
secret redaction, and "analysis never re-runs discovery".
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest
from sqlalchemy import select

from factory.config.youtube_config import YouTubeSettings
from factory.errors import (
    AuthenticationError,
    MalformedResponseError,
    ProviderConfigurationError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderValidationError,
    QuotaExceededError,
)
from factory.intelligence.pipeline import IntelligencePipeline
from factory.jobs.service import JobService
from factory.jobs.types import JobStatus, JobType
from factory.observability.logging import JSONFormatter, SecretRedactionFilter
from factory.observability.usage import CompositeUsageTracker, SQLAlchemyUsageTracker
from factory.providers.youtube.cache import InMemoryDiscoveryCache
from factory.providers.youtube.provider import YouTubeDiscoveryProvider
from factory.schemas.artifacts import (
    AnalysisResult,
    DiscoveryResult,
    OpportunityList,
)
from factory.security.redaction import register_secret, unregister_all_secrets
from factory.storage.models import ProviderUsage
from tests.conftest import ListUsageTracker
from tests.simulated.youtube_server import (
    SIMULATED_API_KEY,
    SimulatedYouTubeBehavior,
    SimulatedYouTubeServer,
)

pytestmark = [pytest.mark.simulated, pytest.mark.offline]

WRONG_API_KEY = "AIzaSyWRONG_SIMULATED_KEY_0000000002"  # fake value, offline only

DISCOVERY_PAYLOAD: dict[str, Any] = {
    "query": "history mystery",
    "language": "en",
    "target_audience": "history enthusiasts",
    "result_limit": 20,
    "page_size": 10,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def yt_server():
    """A running local simulation of the YouTube Data API v3."""
    with SimulatedYouTubeServer() as server:
        yield server


@pytest.fixture
def yt_settings(yt_server: SimulatedYouTubeServer) -> YouTubeSettings:
    return YouTubeSettings(
        youtube_API_KEY=SIMULATED_API_KEY,
        base_url=yt_server.base_url,
        discovery_result_limit=50,
        search_max_results_per_page=10,
        quota_budget_units_per_run=1000,
        max_retries=2,
    )


@pytest.fixture
def yt_usage() -> ListUsageTracker:
    return ListUsageTracker()


@pytest.fixture
def yt_provider(
    yt_settings: YouTubeSettings, yt_usage: ListUsageTracker, session_factory
) -> YouTubeDiscoveryProvider:
    """The production provider against the simulation.

    Usage goes to BOTH an in-memory tracker (easy assertions) and the
    database (persistence assertions) through a composite tracker.
    """
    tracker = CompositeUsageTracker([yt_usage, SQLAlchemyUsageTracker(session_factory)])
    return YouTubeDiscoveryProvider(
        yt_settings,
        usage_tracker=tracker,
        cache=InMemoryDiscoveryCache(),
    )


@pytest.fixture
def pipeline(yt_provider, yt_settings, artifact_store) -> IntelligencePipeline:
    """The production intelligence pipeline against the simulation."""
    return IntelligencePipeline(
        provider=yt_provider,
        artifact_store=artifact_store,
        youtube_settings=yt_settings,
    )


@pytest.fixture
def job_service(projects) -> JobService:
    from factory.jobs.service import JobService as _JobService

    return _JobService(projects, retry_base_seconds=1, retry_max_seconds=5)


def _run(pipeline: IntelligencePipeline, service: JobService, job_type: JobType, **kwargs: Any):
    job = service.enqueue(job_type, project_id="proj-1", **kwargs)
    return pipeline.run_job(service, job.id), job


# ---------------------------------------------------------------------------
# The full pipeline
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_discovery_analysis_opportunities_end_to_end(
        self,
        pipeline: IntelligencePipeline,
        job_service: JobService,
        artifact_store,
        yt_server: SimulatedYouTubeServer,
        yt_usage: ListUsageTracker,
        session_factory,
    ) -> None:
        # --- discovery ---
        record, _job = _run(
            pipeline, job_service, JobType.YOUTUBE_DISCOVERY, payload=DISCOVERY_PAYLOAD
        )
        assert record.status == JobStatus.SUCCEEDED
        assert record.output_artifact_id

        _rec, discovery_payload = artifact_store.load(record.output_artifact_id)
        discovery = DiscoveryResult.model_validate(discovery_payload)
        assert discovery.project_id == "proj-1"
        assert discovery.query == "history mystery"
        assert discovery.language == "en"
        assert discovery.target_audience == "history enthusiasts"
        assert len(discovery.videos) == 20  # result limit honored
        assert discovery.channels  # channel metrics collected
        # Normalization checks:
        first = discovery.videos[0]
        assert first.url.startswith("https://www.youtube.com/watch?v=")
        assert first.published_at is not None
        assert first.views > 0
        assert first.channel_title
        # Quota accounting: 2 search pages (2 x 100) + 1 videos call + 1
        # channels call = 202 documented units.
        assert discovery.quota_units_used == 202
        assert discovery.provider["name"] == "youtube"
        # Pagination happened: 2 search calls for 20 videos at page size 10.
        assert yt_server.state.count(endpoint="search") == 2
        assert yt_server.state.count(endpoint="videos") == 1
        assert yt_server.state.count(endpoint="channels") == 1
        # Usage records with quota metadata (never the API key):
        assert len(yt_usage.records) == 4
        for usage_record in yt_usage.records:
            assert usage_record.provider == "youtube"
            assert usage_record.metadata is not None
            assert usage_record.metadata["quota_units"] in (100, 1)
            assert usage_record.usage_available is False
            assert SIMULATED_API_KEY not in json.dumps(usage_record.to_dict())

        # --- market analysis (input: the discovery artifact) ---
        search_count_before = yt_server.state.count(endpoint="search")
        record, _job = _run(
            pipeline,
            job_service,
            JobType.MARKET_ANALYSIS,
            payload={"discovery_artifact_id": _rec.id},
        )
        assert record.status == JobStatus.SUCCEEDED
        # Analysis never re-runs discovery:
        assert yt_server.state.count(endpoint="search") == search_count_before

        _rec2, analysis_payload = artifact_store.load(record.output_artifact_id)
        analysis = AnalysisResult.model_validate(analysis_payload)
        assert analysis.video_count == 20
        assert analysis.channel_count == 5
        assert analysis.source_artifact_id == _rec.id
        assert len(analysis.videos) == 20  # per-video evidence preserved
        assert analysis.scoring["weights"]  # scoring is explained
        # Evidence: sub-scores present, composites in bounds, ranked:
        for video in analysis.videos:
            assert 0.0 <= video.composite_score <= 100.0
            assert video.sub_scores
        assert analysis.top_performer_video_ids
        assert analysis.competition["channel_count"] == 5
        assert analysis.patterns  # topic/format/question patterns

        # --- opportunity detection (input: the analysis artifact) ---
        record, _job = _run(
            pipeline,
            job_service,
            JobType.OPPORTUNITY_DETECTION,
            payload={"analysis_artifact_id": _rec2.id},
        )
        assert record.status == JobStatus.SUCCEEDED
        _rec3, opportunities_payload = artifact_store.load(record.output_artifact_id)
        opportunity_list = OpportunityList.model_validate(opportunities_payload)
        assert opportunity_list.source_artifact_id == _rec2.id
        assert opportunity_list.opportunities  # opportunities generated
        assert opportunity_list.clusters  # clustering summaries included

        # The underserved theme ("ancient aqueducts": recurring, low views)
        # must surface as an opportunity with a novelty rationale:
        aqueduct_opps = [o for o in opportunity_list.opportunities if "aqueduct" in o.topic]
        assert aqueduct_opps, "underserved recurring topic must become an opportunity"
        best = aqueduct_opps[0]
        assert best.competition_signals["saturation"] == "underserved"
        assert best.novelty_rationale
        assert best.confidence > 0
        assert best.supporting_video_ids
        assert best.evidence_refs
        assert best.recommended_angle
        assert best.audience_question
        # Scores are ordered and bounded:
        scores = [o.score for o in opportunity_list.opportunities]
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 <= s <= 100.0 for s in scores)
        # Deterministic ids:
        assert all(o.opportunity_id for o in opportunity_list.opportunities)

        # --- usage persisted with quota metadata ---
        with session_factory() as session:
            rows = session.execute(select(ProviderUsage)).scalars().all()
        assert len(rows) == 4
        for row in rows:
            assert row.provider == "youtube"
            assert row.provider_metadata is not None
            assert row.provider_metadata["quota_units"] in (100, 1)
            assert row.success is True

    def test_artifact_integrity_tamper_detected(
        self,
        pipeline: IntelligencePipeline,
        job_service: JobService,
        artifact_store,
    ) -> None:
        from factory.errors import ArtifactIntegrityError

        record, _job = _run(
            pipeline, job_service, JobType.YOUTUBE_DISCOVERY, payload=DISCOVERY_PAYLOAD
        )
        assert record.status == JobStatus.SUCCEEDED
        artifact_record, _payload = artifact_store.load(record.output_artifact_id)
        path = artifact_store.resolve_path(artifact_record.storage_ref)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["query"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ArtifactIntegrityError):
            artifact_store.load(record.output_artifact_id)


# ---------------------------------------------------------------------------
# Provider behavior against the simulation
# ---------------------------------------------------------------------------


class TestProviderBehavior:
    def test_pagination_and_dedup(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        page = yt_provider.discover_videos(
            {"q": "history mystery"}, max_results=25, job_id="job-pag"
        )
        # 25 results at page size 10 → 3 pages, deduplicated:
        assert len(page.items) == 25
        assert len({item.video_id for item in page.items}) == 25
        assert yt_server.state.count(endpoint="search") == 3
        assert page.quota_units_used == 300  # 3 search calls x 100

    def test_video_and_channel_metrics_normalized(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        page = yt_provider.discover_videos({"q": "aqueducts"}, max_results=5, job_id="job-m")
        ids = [item.video_id for item in page.items]
        videos = yt_provider.get_video_metrics(ids, job_id="job-m")
        assert len(videos) == 5
        for video in videos:
            assert video.views > 0
            assert video.duration_seconds is not None
            assert video.likes >= 0 and video.comments >= 0
            assert video.published_at is not None
            assert video.description_chars is not None
        channels = yt_provider.get_channel_metrics(
            [video.channel_id for video in videos], job_id="job-m"
        )
        assert channels
        for channel in channels:
            assert channel.subscriber_count is not None
            assert channel.view_count is not None
            assert channel.video_count is not None
        # Quota accounting across all calls:
        assert yt_provider.quota_used() == 100 + 1 + 1

    def test_search_cache_avoids_repeat_calls(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        yt_provider.discover_videos({"q": "bunkers"}, max_results=10, job_id="job-c1")
        count_after_first = yt_server.state.count(endpoint="search")
        yt_provider.discover_videos({"q": "bunkers"}, max_results=10, job_id="job-c2")
        assert yt_server.state.count(endpoint="search") == count_after_first  # cached

    def test_quota_budget_enforced_before_calls(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_settings: YouTubeSettings,
        yt_usage: ListUsageTracker,
    ) -> None:
        # Budget of 150 units: one search (100) fits, the second page (another
        # 100) would exceed it → stopped before the API call.
        tight = yt_settings.model_copy(update={"quota_budget_units_per_run": 150})
        provider = YouTubeDiscoveryProvider(
            tight, usage_tracker=yt_usage, cache=InMemoryDiscoveryCache()
        )
        with pytest.raises(QuotaExceededError):
            provider.discover_videos({"q": "history mystery"}, max_results=20, job_id="job-q")
        assert yt_server.state.count(endpoint="search") == 1  # only one call made


class TestErrorHandling:
    def test_authentication_failure(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_settings: YouTubeSettings,
        yt_usage: ListUsageTracker,
    ) -> None:
        bad = yt_settings.model_copy(update={"youtube_API_KEY": WRONG_API_KEY})
        provider = YouTubeDiscoveryProvider(
            bad, usage_tracker=yt_usage, cache=InMemoryDiscoveryCache()
        )
        with pytest.raises(AuthenticationError) as exc_info:
            provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-auth")
        assert exc_info.value.error_code == "keyInvalid"
        assert yt_server.state.requests  # the attempt was observed
        assert all(not r.authorized for r in yt_server.state.requests)

    def test_missing_key_fails_safely(self, yt_server: SimulatedYouTubeServer) -> None:
        from factory.providers.youtube.client import YouTubeClient

        settings = YouTubeSettings(youtube_API_KEY=None, base_url=yt_server.base_url)
        with pytest.raises(ProviderConfigurationError):
            YouTubeClient(settings)
        with pytest.raises(ProviderConfigurationError):
            YouTubeDiscoveryProvider(settings)
        assert yt_server.state.requests == []  # nothing was sent

    def test_quota_exceeded_from_api(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        yt_server.state.queue(SimulatedYouTubeBehavior(status=403))
        with pytest.raises(QuotaExceededError):
            yt_provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-403")

    def test_rate_limit_retried_then_succeeds(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        yt_server.state.queue(SimulatedYouTubeBehavior(status=429))
        page = yt_provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-429")
        assert len(page.items) == 5
        # 1 initial attempt + 1 retry:
        assert yt_server.state.count(endpoint="search") == 2

    def test_5xx_retried_then_raises(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        yt_server.state.queue(
            SimulatedYouTubeBehavior(status=500),
            SimulatedYouTubeBehavior(status=500),
            SimulatedYouTubeBehavior(status=500),
        )
        with pytest.raises(ProviderHTTPError) as exc_info:
            yt_provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-500")
        assert exc_info.value.status_code == 500
        assert yt_server.state.count(endpoint="search") == 3  # 1 + 2 retries

    def test_timeout(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_settings: YouTubeSettings,
        yt_usage: ListUsageTracker,
    ) -> None:
        fast = yt_settings.model_copy(update={"timeout_seconds": 0.5})
        provider = YouTubeDiscoveryProvider(
            fast, usage_tracker=yt_usage, cache=InMemoryDiscoveryCache()
        )
        yt_server.state.queue(SimulatedYouTubeBehavior(delay_seconds=2.0))
        with pytest.raises(ProviderTimeoutError):
            provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-to")

    def test_malformed_response(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        yt_server.state.queue(SimulatedYouTubeBehavior(mode="invalid_json"))
        with pytest.raises(MalformedResponseError):
            yt_provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-bad")

    def test_missing_items_is_malformed(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        yt_server.state.queue(SimulatedYouTubeBehavior(mode="missing_items"))
        with pytest.raises(MalformedResponseError):
            yt_provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-noitems")

    def test_invalid_request_parameters(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_provider: YouTubeDiscoveryProvider,
    ) -> None:
        # A missing id parameter on /videos is a documented 400
        # (invalidParameter) — force a raw call with an empty id list:
        with pytest.raises(ProviderValidationError) as exc_info:
            yt_provider._client._request(
                "GET", "/videos", {"part": "snippet", "id": ""}, endpoint="videos", job_id="job-400"
            )
        assert exc_info.value.error_code == "invalidParameter"


# ---------------------------------------------------------------------------
# Job-system integration against the simulation
# ---------------------------------------------------------------------------


class TestJobIntegration:
    def test_checkpoint_resume_continues_pagination(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_settings: YouTubeSettings,
        yt_usage: ListUsageTracker,
        session_factory,
        artifact_store,
        projects,
    ) -> None:
        # Cache DISABLED so resume genuinely re-requests only the missing page.
        no_cache = yt_settings.model_copy(update={"cache_enabled": False})
        provider = YouTubeDiscoveryProvider(no_cache, usage_tracker=yt_usage, cache=None)
        pipeline = IntelligencePipeline(
            provider=provider,
            artifact_store=artifact_store,
            youtube_settings=no_cache,
        )
        service = JobService(session_factory, retry_base_seconds=1, retry_max_seconds=5)

        # Page 1 succeeds; page 2 fails all attempts (3 x 500) → the job fails
        # and is rescheduled (PENDING with a retry time).
        yt_server.state.queue(
            SimulatedYouTubeBehavior(),  # page 1: success
            SimulatedYouTubeBehavior(status=500),
            SimulatedYouTubeBehavior(status=500),
            SimulatedYouTubeBehavior(status=500),
        )
        job = service.enqueue(
            JobType.YOUTUBE_DISCOVERY,
            project_id="proj-1",
            payload={**DISCOVERY_PAYLOAD, "result_limit": 20, "page_size": 10},
        )
        record = pipeline.run_job(service, job.id)
        assert record.status == JobStatus.PENDING  # retry scheduled
        assert record.error is not None
        assert record.error.type == "ProviderHTTPError"
        page1_count = yt_server.state.count(endpoint="search")
        assert page1_count == 4  # page 1 (1) + page 2 attempts (3)

        # Re-run the pending job: it resumes from the checkpoint (page 1 is
        # NOT re-requested).
        record = pipeline.run_job(service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        assert record.output_artifact_id
        # Page 1 was requested exactly once across both runs:
        page1_requests = sum(
            1
            for r in yt_server.state.requests
            if r.endpoint == "search" and "pageToken" not in r.params
        )
        assert page1_requests == 1

    def test_failed_analysis_does_not_rerun_discovery(
        self,
        pipeline: IntelligencePipeline,
        job_service: JobService,
        artifact_store,
        yt_server: SimulatedYouTubeServer,
        monkeypatch: pytest.MonkeyPatch,
        projects,
    ) -> None:
        record, _job = _run(
            pipeline, job_service, JobType.YOUTUBE_DISCOVERY, payload=DISCOVERY_PAYLOAD
        )
        assert record.status == JobStatus.SUCCEEDED
        search_count = yt_server.state.count(endpoint="search")

        # Make the analysis handler fail once, then succeed on retry.
        original = pipeline.analysis_handler
        calls = {"count": 0}

        def flaky_handler(context):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("simulated analysis outage")
            return original(context)

        monkeypatch.setattr(pipeline, "analysis_handler", flaky_handler)
        pipeline.handlers[JobType.MARKET_ANALYSIS] = flaky_handler

        analysis_job = job_service.enqueue(
            JobType.MARKET_ANALYSIS,
            project_id="proj-1",
            payload={"discovery_artifact_id": record.output_artifact_id},
        )
        failed = pipeline.run_job(job_service, analysis_job.id)
        assert failed.status == JobStatus.PENDING  # retry scheduled
        assert failed.error is not None and failed.error.type == "RuntimeError"

        # Re-run the pending job: it retries against the same artifact.
        succeeded = pipeline.run_job(job_service, analysis_job.id)
        assert succeeded.status == JobStatus.SUCCEEDED
        assert succeeded.output_artifact_id
        # Discovery never re-ran:
        assert yt_server.state.count(endpoint="search") == search_count

    def test_idempotent_enqueue(
        self,
        pipeline: IntelligencePipeline,
        job_service: JobService,
    ) -> None:
        first = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY,
            project_id="proj-1",
            payload=DISCOVERY_PAYLOAD,
            idempotency_key="discovery-run-1",
        )
        second = job_service.enqueue(
            JobType.YOUTUBE_DISCOVERY,
            project_id="proj-1",
            payload=DISCOVERY_PAYLOAD,
            idempotency_key="discovery-run-1",
        )
        assert first.id == second.id

    def test_secret_redaction(
        self,
        yt_server: SimulatedYouTubeServer,
        yt_settings: YouTubeSettings,
        yt_usage: ListUsageTracker,
    ) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(SecretRedactionFilter())
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        register_secret(SIMULATED_API_KEY)
        register_secret(WRONG_API_KEY)
        try:
            provider = YouTubeDiscoveryProvider(
                yt_settings, usage_tracker=yt_usage, cache=InMemoryDiscoveryCache()
            )
            provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-secret")

            bad = yt_settings.model_copy(update={"youtube_API_KEY": WRONG_API_KEY})
            bad_provider = YouTubeDiscoveryProvider(
                bad, usage_tracker=yt_usage, cache=InMemoryDiscoveryCache()
            )
            with pytest.raises(AuthenticationError) as exc_info:
                bad_provider.discover_videos({"q": "tunnels"}, max_results=5, job_id="job-secret")

            # Simulate an accidental leak attempt (the key also travels in the
            # URL as the documented `key` query parameter):
            from factory.observability.logging import get_logger

            get_logger("factory.providers.youtube.client").info(
                "debug dump: key is %s", SIMULATED_API_KEY
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
            unregister_all_secrets()

        output = stream.getvalue()
        assert SIMULATED_API_KEY not in output
        assert WRONG_API_KEY not in output
        assert "[REDACTED]" in output
        assert SIMULATED_API_KEY not in str(exc_info.value)
        blob = json.dumps([r.to_dict() for r in yt_usage.records])
        assert SIMULATED_API_KEY not in blob
        assert WRONG_API_KEY not in blob

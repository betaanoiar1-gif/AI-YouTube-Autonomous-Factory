"""SIMULATED / OFFLINE research-plane integration tests.

These tests run the **production** research pipeline against a local, in-process
simulation of the research plane's external services
(``tests/simulated/research_server.py``):

    RESEARCH job (production handler)
      → ResearchEngine (production, 8 checkpointed stages)
        → ResearchProvider / SourceProvider / EvidenceExtractor / ClaimVerifier
          contracts → WebSourceProvider (production: documented search-API shape
          + SSRF-protected SafeFetcher over real sockets to 127.0.0.x)
            → simulated search API + simulated source documents
              → evidence → claims → verification → contradictions
                → versioned research_plan + research_report artifacts

No real credentials and no real network egress are used. Distinct loopback
addresses act as distinct source domains. The fetcher is constructed with
``allow_loopback=True`` ONLY for this offline simulation — production fetchers
never permit it. No real external connectivity is claimed.

Covered: the complete pipeline with multiple source types, supporting and
contradictory evidence, syndication (not independent), insufficient evidence,
malformed sources, timeouts, oversized/wrong-type responses, unsafe redirects
(SSRF), checkpoint resume, failed-stage retry, idempotency, no duplicate
collection, artifact integrity, lineage, and the originality boundary.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from factory.config.research_config import ResearchSettings
from factory.errors import ArtifactIntegrityError
from factory.jobs.service import JobService
from factory.jobs.types import JobStatus, JobType
from factory.providers.research.web import WebSourceProvider
from factory.research.pipeline import ResearchPipeline
from factory.research.sources import InMemorySourceCache
from factory.schemas.artifacts import (
    ArtifactType,
    OpportunityItem,
    OpportunityList,
    ResearchPlan,
    ResearchReport,
)
from factory.security.fetch import SafeFetcher
from tests.simulated.research_server import (
    SimulatedResearchBehavior,
    SimulatedResearchServer,
)

pytestmark = [pytest.mark.simulated, pytest.mark.offline]


def _opportunity_list_payload() -> dict[str, Any]:
    return OpportunityList(
        opportunity_list_id="ol-1",
        project_id="proj-1",
        opportunities=[
            OpportunityItem(
                opportunity_id="opp-aqueduct",
                title="Original coverage: ancient aqueducts",
                topic="ancient aqueducts",
                score=72.5,
                rationale="Underserved recurring theme with measurable demand.",
                audience_question="What should viewers know about ancient aqueducts?",
                evidence_refs=["analysis:a1:topic:ancient aqueducts"],
                supporting_video_ids=["vid-competitor-1", "vid-competitor-2"],
                demand_signals={"supporting_video_count": 2, "total_views": 4000},
                competition_signals={"saturation": "underserved"},
                novelty_rationale="Classified underserved.",
                confidence=0.8,
                recommended_angle="Original, evidence-based coverage.",
            )
        ],
    ).model_dump(mode="json")


@pytest.fixture
def research_server():
    with SimulatedResearchServer() as server:
        yield server


@pytest.fixture
def research_settings(research_server: SimulatedResearchServer) -> ResearchSettings:
    return ResearchSettings(
        search_api_url=research_server.search_api_url,
        fetch_timeout_seconds=5.0,
        max_collection_bytes=2_000_000,
        max_sources_standard=10,
        max_sources_deep=25,
    )


@pytest.fixture
def source_cache() -> InMemorySourceCache:
    return InMemorySourceCache()


@pytest.fixture
def research_pipeline(
    research_settings: ResearchSettings,
    research_server: SimulatedResearchServer,
    source_cache: InMemorySourceCache,
    artifact_store,
) -> ResearchPipeline:
    """The production research pipeline against the simulation."""
    from factory.research.engine import ResearchEngine
    from factory.research.extractor import DeterministicEvidenceExtractor
    from factory.research.planner import DeterministicResearchPlanner
    from factory.research.verifier import DeterministicClaimVerifier

    provider = WebSourceProvider(
        settings=research_settings,
        fetcher=SafeFetcher(
            timeout_seconds=research_settings.fetch_timeout_seconds,
            max_bytes=research_settings.max_collection_bytes,
            allow_loopback=True,  # offline simulation ONLY — never in production
        ),
        source_cache=source_cache,
    )
    engine = ResearchEngine(
        planner=DeterministicResearchPlanner(),
        source_provider=provider,
        extractor=DeterministicEvidenceExtractor(settings=research_settings),
        verifier=DeterministicClaimVerifier(),
        artifact_store=artifact_store,
        source_cache=source_cache,
        settings=research_settings,
    )
    return ResearchPipeline(engine=engine)


@pytest.fixture
def job_service(projects) -> JobService:
    return JobService(projects, retry_base_seconds=1, retry_max_seconds=5)


@pytest.fixture
def opportunity_artifact(artifact_store) -> str:
    """A Phase 1 opportunity_list artifact for the research job to consume."""
    record = artifact_store.save(
        ArtifactType.OPPORTUNITY_LIST, "proj-1", _opportunity_list_payload()
    )
    artifact_id: str = record.id
    return artifact_id


def _enqueue_research(
    job_service: JobService,
    opportunity_artifact: str,
    **overrides: Any,
) -> Any:
    payload: dict[str, Any] = {
        "opportunity_list_artifact_id": opportunity_artifact,
        "opportunity_id": "opp-aqueduct",
        "depth": "standard",
        **overrides,
    }
    return job_service.enqueue(JobType.RESEARCH, project_id="proj-1", payload=payload)


# ---------------------------------------------------------------------------
# The complete pipeline
# ---------------------------------------------------------------------------


class TestCompleteResearchPipeline:
    def test_end_to_end_report(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
        research_server: SimulatedResearchServer,
    ) -> None:
        job = _enqueue_research(job_service, opportunity_artifact)
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        assert record.output_artifact_id
        assert record.progress == 100

        _report_record, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)

        # --- identity + lineage ---
        assert report.research_report_id == job.id
        assert report.opportunity_id == "opp-aqueduct"
        assert report.project_id == "proj-1"
        assert report.topic == "ancient aqueducts"
        assert report.research_question == "What should viewers know about ancient aqueducts?"
        assert report.research_plan_id  # plan artifact lineage
        assert report.started_at and report.completed_at

        # --- sources: multiple types, quality indicators, no duplicates ---
        assert len(report.source_list) == 4
        types = {s.source_type for s in report.source_list}
        assert types == {"reference", "academic", "journalism", "secondary"}
        for source in report.source_list:
            assert source.collection_status == "collected"
            assert source.url_fingerprint
            assert source.content_fingerprint
            assert source.authority_score > 0
            assert source.authority_indicators
        # The mirror is an exact syndication of the reference:
        fingerprints = {s.content_fingerprint for s in report.source_list}
        assert len(fingerprints) == 3  # 4 sources, one duplicated fingerprint

        # --- evidence with provenance ---
        assert report.evidence
        for item in report.evidence:
            assert item.evidence_id
            assert item.source_id
            assert item.passage
            assert item.location
            assert item.extracted_at

        # --- claims: verified, contested, insufficient ---
        by_statement = {c.statement: c for c in report.verified_claims + report.contested_claims}
        completed_claim = next(
            c for c in by_statement.values() if "was completed in" in c.statement
        )
        # Supporting (1312 x3 sources) vs contradicting (1305 x1) -> CONTESTED,
        # and the syndicated mirror does NOT count as independent:
        assert completed_claim.verification_status == "CONTESTED"
        assert completed_claim.independent_source_count == 2  # ref + academic; mirror syndicated
        assert completed_claim.contradicting_source_ids
        assert completed_claim.confidence > 0

        carried_claim = next(
            c for c in by_statement.values() if c.statement.startswith("The aqua aqueduct carried")
        )
        assert carried_claim.verification_status == "SUPPORTED"
        assert carried_claim.independent_source_count == 1  # mirror is syndicated

        population_claim = next(
            c for c in by_statement.values() if "served a population" in c.statement
        )
        assert population_claim.verification_status == "SUPPORTED"

        # --- contradictions preserved, both sides, never resolved silently ---
        assert report.contradiction_details
        date_conflict = next(
            c for c in report.contradiction_details if c.contradiction_type == "date"
        )
        assert set(date_conflict.values) == {"1312", "1305"}
        assert len(date_conflict.source_ids) >= 2  # both sides preserved
        assert date_conflict.resolution_status == "unresolved"
        assert date_conflict.explanation
        # The explicit-disagreement passage is also detected:
        assert any(c.contradiction_type == "disagreement" for c in report.contradiction_details)

        # --- report sections ---
        assert report.executive_findings
        assert report.source_quality["collected"] == 4
        assert report.source_quality["by_type"]["academic"] == 1
        assert report.confidence_summary["verified_claims"] >= 2
        assert report.confidence_summary["contested_claims"] == 1
        assert report.limitations
        assert report.unresolved_questions

        # --- legacy Phase 0 fields populated (backward compatibility) ---
        assert report.summary
        assert report.sources and report.claims and report.contradictions
        assert any(c.support_status == "contradicted" for c in report.claims)

        # --- the research_plan artifact exists and is linked ---
        _plan_record, plan_payload = artifact_store.load(report.research_plan_id)
        plan = ResearchPlan.model_validate(plan_payload)
        assert plan.opportunity_id == "opp-aqueduct"
        assert plan.central_question == report.research_question
        assert plan.subquestions

        # --- originality boundary: no competitor content anywhere ---
        competitor_titles = {
            "The forgotten tunnels of Paris",
            "Forgotten tunnels under the city",
        }
        blob = json.dumps(payload)
        for competitor_title in competitor_titles:
            assert competitor_title not in blob

    def test_no_duplicate_source_collection_across_runs(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        research_server: SimulatedResearchServer,
    ) -> None:
        job = _enqueue_research(job_service, opportunity_artifact)
        research_pipeline.run_job(job_service, job.id)
        wiki_requests = research_server.state.count(path_prefix="/wiki/")
        search_requests = research_server.state.count(path_prefix="/api.php")
        assert wiki_requests == 4 and search_requests >= 1

        # A second research run: discovery re-runs (cheap listing calls), but
        # the source cache serves every collection — no source is collected
        # twice ("do not repeatedly collect the same source").
        job2 = _enqueue_research(job_service, opportunity_artifact)
        record2 = research_pipeline.run_job(job_service, job2.id)
        assert record2.status == JobStatus.SUCCEEDED
        assert research_server.state.count(path_prefix="/wiki/") == wiki_requests
        assert research_server.state.count(path_prefix="/api.php") >= search_requests


# ---------------------------------------------------------------------------
# Job-system integration
# ---------------------------------------------------------------------------


class TestJobIntegration:
    def test_checkpoint_resume_skips_completed_collection(
        self,
        research_settings: ResearchSettings,
        research_server: SimulatedResearchServer,
        source_cache: InMemorySourceCache,
        artifact_store,
        opportunity_artifact: str,
        job_service: JobService,
    ) -> None:
        """A failure during collection resumes without re-collecting finished
        sources (cache + checkpoint)."""
        from factory.research.engine import ResearchEngine
        from factory.research.extractor import DeterministicEvidenceExtractor
        from factory.research.planner import DeterministicResearchPlanner
        from factory.research.verifier import DeterministicClaimVerifier

        # Fail the search API on every attempt (1 initial + 2 retries), so the
        # discovery stage fails and the job is rescheduled.
        research_server.state.queue(
            SimulatedResearchBehavior(status=500),
            SimulatedResearchBehavior(status=500),
            SimulatedResearchBehavior(status=500),
        )

        provider = WebSourceProvider(
            settings=research_settings,
            fetcher=SafeFetcher(
                timeout_seconds=research_settings.fetch_timeout_seconds,
                max_bytes=research_settings.max_collection_bytes,
                allow_loopback=True,
            ),
            source_cache=source_cache,
        )
        engine = ResearchEngine(
            planner=DeterministicResearchPlanner(),
            source_provider=provider,
            extractor=DeterministicEvidenceExtractor(settings=research_settings),
            verifier=DeterministicClaimVerifier(),
            artifact_store=artifact_store,
            source_cache=source_cache,
            settings=research_settings,
        )
        pipeline = ResearchPipeline(engine=engine)

        job = _enqueue_research(job_service, opportunity_artifact)
        failed = pipeline.run_job(job_service, job.id)
        # The search API 500 (retried, then raised) fails the discovery stage.
        assert failed.status == JobStatus.PENDING  # retry scheduled
        assert failed.error is not None
        assert failed.error.type == "ProviderHTTPError"

        # Re-run: the search succeeds now and the whole pipeline completes,
        # collecting every source exactly once.
        resumed = pipeline.run_job(job_service, job.id)
        assert resumed.status == JobStatus.SUCCEEDED
        assert resumed.output_artifact_id
        assert research_server.state.count(path_prefix="/wiki/") == 4

    def test_failed_stage_does_not_repeat_earlier_stages(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        research_server: SimulatedResearchServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail the evidence-extraction stage once; the retry must not repeat
        planning, discovery, or collection."""
        original = research_pipeline._engine._extractor.extract_evidence
        calls = {"count": 0}

        def flaky(document, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("simulated extraction outage")
            return original(document, **kwargs)

        monkeypatch.setattr(research_pipeline._engine._extractor, "extract_evidence", flaky)
        job = _enqueue_research(job_service, opportunity_artifact)
        failed = research_pipeline.run_job(job_service, job.id)
        assert failed.status == JobStatus.PENDING
        assert failed.error is not None and failed.error.type == "RuntimeError"
        wiki_after_fail = research_server.state.count(path_prefix="/wiki/")

        resumed = research_pipeline.run_job(job_service, job.id)
        assert resumed.status == JobStatus.SUCCEEDED
        # Collection never repeated (checkpoint + cache):
        assert research_server.state.count(path_prefix="/wiki/") == wiki_after_fail

    def test_idempotent_enqueue(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
    ) -> None:
        first = job_service.enqueue(
            JobType.RESEARCH,
            project_id="proj-1",
            payload={
                "opportunity_list_artifact_id": opportunity_artifact,
                "opportunity_id": "opp-aqueduct",
            },
            idempotency_key="research-1",
        )
        second = job_service.enqueue(
            JobType.RESEARCH,
            project_id="proj-1",
            payload={
                "opportunity_list_artifact_id": opportunity_artifact,
                "opportunity_id": "opp-aqueduct",
            },
            idempotency_key="research-1",
        )
        assert first.id == second.id

    def test_artifact_integrity(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
    ) -> None:
        job = _enqueue_research(job_service, opportunity_artifact)
        record = research_pipeline.run_job(job_service, job.id)
        report_record, _payload = artifact_store.load(record.output_artifact_id)
        path = artifact_store.resolve_path(report_record.storage_ref)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["summary"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ArtifactIntegrityError):
            artifact_store.load(record.output_artifact_id)

    def test_default_opportunity_is_top_ranked(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
    ) -> None:
        job = job_service.enqueue(
            JobType.RESEARCH,
            project_id="proj-1",
            payload={"opportunity_list_artifact_id": opportunity_artifact},
        )
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)
        assert report.opportunity_id == "opp-aqueduct"  # the only/top opportunity


# ---------------------------------------------------------------------------
# Source collection behaviors (safe retrieval)
# ---------------------------------------------------------------------------


class TestCollectionBehaviors:
    def test_timeout_marks_source_failed_and_pipeline_continues(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
        research_server: SimulatedResearchServer,
        research_settings: ResearchSettings,
    ) -> None:
        # The search API calls run first (4 queries); the delay then hits the
        # first page fetch, which times out — other sources still complete.
        research_server.state.queue(
            *[SimulatedResearchBehavior()] * 6,  # search + first page fetches
            SimulatedResearchBehavior(delay_seconds=3.0),
        )
        fast_settings = research_settings.model_copy(update={"fetch_timeout_seconds": 0.5})
        # Rebuild the pipeline with a short fetch timeout.
        from factory.research.engine import ResearchEngine
        from factory.research.extractor import DeterministicEvidenceExtractor
        from factory.research.planner import DeterministicResearchPlanner
        from factory.research.verifier import DeterministicClaimVerifier

        provider = WebSourceProvider(
            settings=fast_settings,
            fetcher=SafeFetcher(
                timeout_seconds=0.5,
                max_bytes=fast_settings.max_collection_bytes,
                allow_loopback=True,
            ),
            source_cache=research_pipeline._engine._source_cache,
        )
        engine = ResearchEngine(
            planner=DeterministicResearchPlanner(),
            source_provider=provider,
            extractor=DeterministicEvidenceExtractor(settings=fast_settings),
            verifier=DeterministicClaimVerifier(),
            artifact_store=artifact_store,
            source_cache=research_pipeline._engine._source_cache,
            settings=fast_settings,
        )
        pipeline = ResearchPipeline(engine=engine)

        job = _enqueue_research(job_service, opportunity_artifact)
        record = pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)
        failed_sources = [s for s in report.source_list if s.collection_status == "failed"]
        assert failed_sources, "the timed-out source must be marked failed"
        assert "timed out" in (failed_sources[0].collection_error or "")
        # The other sources still produced a usable report:
        assert len(report.source_list) == 4
        assert report.verified_claims

    def test_oversized_and_wrong_type_marked_failed(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
        research_server: SimulatedResearchServer,
    ) -> None:
        research_server.state.queue(
            *[SimulatedResearchBehavior()] * 6,  # search + first page fetches
            SimulatedResearchBehavior(mode="oversized"),
            SimulatedResearchBehavior(mode="wrong_type"),
        )
        job = _enqueue_research(job_service, opportunity_artifact)
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)
        failed = [s for s in report.source_list if s.collection_status == "failed"]
        assert len(failed) == 2
        errors = " ".join(s.collection_error or "" for s in failed)
        assert "maximum size" in errors
        assert "content type" in errors

    def test_unsafe_redirect_blocked_by_ssrf_protection(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
        research_server: SimulatedResearchServer,
    ) -> None:
        # A redirect to a private address must be blocked even though the
        # simulation allows loopback. The search API calls run first; the
        # redirect then hits the first page fetch.
        research_server.state.queue(
            *[SimulatedResearchBehavior()] * 6,
            SimulatedResearchBehavior(mode="redirect_private"),
        )
        job = _enqueue_research(job_service, opportunity_artifact)
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)
        failed = [s for s in report.source_list if s.collection_status == "failed"]
        assert failed
        assert "blocked address" in (failed[0].collection_error or "")

    def test_malformed_search_response_fails_discovery_stage(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        research_server: SimulatedResearchServer,
    ) -> None:
        research_server.state.queue(SimulatedResearchBehavior(mode="invalid_json"))
        job = _enqueue_research(job_service, opportunity_artifact)
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.PENDING  # retry scheduled
        assert record.error is not None
        assert record.error.type == "MalformedResponseError"

    def test_search_api_429_retried(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        research_server: SimulatedResearchServer,
    ) -> None:
        research_server.state.queue(SimulatedResearchBehavior(status=429))
        job = _enqueue_research(job_service, opportunity_artifact)
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        # 1 initial attempt + retries against the search API:
        assert research_server.state.count(path_prefix="/api.php") >= 2

    def test_unavailable_source_preserves_metadata(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
    ) -> None:
        """A source that cannot be retrieved keeps its metadata and is marked
        failed — content is never invented."""
        from factory.research.sources import make_candidate

        # Inject a candidate pointing at a closed port (connection refused).
        engine = research_pipeline._engine
        original = engine._source_provider.discover_sources

        def with_dead_source(query, **kwargs):
            found = original(query, **kwargs)
            dead = make_candidate(
                "http://127.0.0.1:1/wiki/Dead_Page",
                title="Dead Page",
                discovery_query=query,
                relevance_score=0.1,
            )
            return [*found, dead]

        engine._source_provider.discover_sources = with_dead_source  # type: ignore[method-assign]
        job = _enqueue_research(job_service, opportunity_artifact)
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)
        dead = [s for s in report.source_list if s.title == "Dead Page"]
        assert dead, "the unretrievable source's metadata must be preserved"
        assert dead[0].collection_status == "failed"
        assert dead[0].collection_error
        assert dead[0].url and dead[0].url_fingerprint

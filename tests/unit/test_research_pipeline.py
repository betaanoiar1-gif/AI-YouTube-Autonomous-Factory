"""Research pipeline unit tests (job wiring + wiring of the default pipeline)."""

from __future__ import annotations

import pytest

from factory.config.research_config import ResearchSettings
from factory.jobs.service import JobService
from factory.jobs.types import JobStatus, JobType
from factory.providers.research.base import (
    ClaimVerifier,
    EvidenceExtractor,
    ResearchProvider,
    SourceProvider,
)
from factory.providers.research.types import (
    CollectedSource,
    VerificationResult,
)
from factory.research.pipeline import ResearchPipeline, build_default_research_pipeline
from factory.schemas.artifacts import ArtifactType, OpportunityItem, OpportunityList, ResearchReport


class StubPlanner(ResearchProvider):
    name = "stub-planner"

    def plan_research(
        self,
        opportunity,
        *,
        project_id,
        depth="standard",
        language=None,
        target_audience=None,
        job_id=None,
    ):
        from factory.schemas.artifacts import ResearchPlan, ResearchSubquestion

        return ResearchPlan(
            research_plan_id=f"plan-{job_id}",
            project_id=project_id,
            opportunity_id=opportunity.opportunity_id,
            central_question=opportunity.audience_question or "q?",
            subquestions=[ResearchSubquestion(question="q1", category="historical")],
            required_facts=["f1"],
            source_requirements=["reference"],
            verification_requirements=["two sources"],
            depth=depth,
            language=language,
            target_audience=target_audience,
        )


class StubSourceProvider(SourceProvider):
    name = "stub-sources"

    def discover_sources(self, query, *, context=None, limit=10, source_types=None, job_id=None):
        from factory.research.sources import make_candidate

        return [make_candidate("https://example.com/a", title="A", discovery_query=query)]

    def collect_source(self, source, *, job_id=None):
        return CollectedSource(
            candidate=source,
            content="The Aqua Aqueduct was completed in 1312.",
            content_type="text/plain",
            byte_size=40,
            content_fingerprint="f" * 64,
            collection_status="collected",
        )


class StubExtractor(EvidenceExtractor):
    name = "stub-extractor"

    def extract_evidence(self, document, *, plan=None, job_id=None):
        from factory.schemas.artifacts import EvidenceItem

        return [
            EvidenceItem(
                evidence_id="ev-1",
                source_id=f"src-{document.candidate.url_fingerprint[:16]}",
                claim="The Aqua Aqueduct was completed in 1312.",
                confidence=0.9,
                subject="aqua aqueduct",
                predicate="was completed in",
                value="1312",
                value_type="date",
            )
        ]


class StubVerifier(ClaimVerifier):
    name = "stub-verifier"

    def verify_claims(self, claims, *, sources, job_id=None):
        return [
            VerificationResult(
                claim_id=f"cl-{i:04d}",
                verification_status="SUPPORTED",
                confidence=0.8,
                supporting_source_ids=[f"src-{s.candidate.url_fingerprint[:16]}" for s in sources],
                independent_source_count=len(sources),
                evidence_coverage=1.0,
            )
            for i, _ in enumerate(claims)
        ]


@pytest.fixture
def opportunity_artifact(artifact_store) -> str:
    payload = OpportunityList(
        opportunity_list_id="ol-1",
        project_id="proj-1",
        opportunities=[
            OpportunityItem(
                opportunity_id="opp-1",
                title="Original coverage: ancient aqueducts",
                topic="ancient aqueducts",
                score=72.5,
                rationale="Underserved theme.",
                audience_question="What should viewers know about ancient aqueducts?",
                confidence=0.8,
            )
        ],
    ).model_dump(mode="json")
    record = artifact_store.save(ArtifactType.OPPORTUNITY_LIST, "proj-1", payload)
    artifact_id: str = record.id
    return artifact_id


@pytest.fixture
def research_pipeline(artifact_store, session_factory) -> ResearchPipeline:
    return build_default_research_pipeline(
        session_factory=session_factory,
        artifact_store=artifact_store,
        research_settings=ResearchSettings(max_sources_standard=5),
        source_provider=StubSourceProvider(),
        planner=StubPlanner(),
        extractor=StubExtractor(),
        verifier=StubVerifier(),
    )


@pytest.fixture
def job_service(projects) -> JobService:
    return JobService(projects, retry_base_seconds=1, retry_max_seconds=5)


class TestResearchPipelineWiring:
    def test_handlers_registered(self, research_pipeline: ResearchPipeline) -> None:
        assert JobType.RESEARCH in research_pipeline.handlers

    def test_research_job_end_to_end(
        self,
        research_pipeline: ResearchPipeline,
        job_service: JobService,
        opportunity_artifact: str,
        artifact_store,
    ) -> None:
        job = job_service.enqueue(
            JobType.RESEARCH,
            project_id="proj-1",
            payload={
                "opportunity_list_artifact_id": opportunity_artifact,
                "opportunity_id": "opp-1",
            },
        )
        record = research_pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        assert record.output_artifact_id
        _rec, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)
        assert report.opportunity_id == "opp-1"
        assert report.verified_claims

    def test_unknown_job_type_rejected(
        self, research_pipeline: ResearchPipeline, job_service: JobService
    ) -> None:
        job = job_service.enqueue(JobType.SCRIPT, project_id="proj-1", payload={})
        with pytest.raises(ValueError):
            research_pipeline.run_job(job_service, job.id)

    def test_default_pipeline_builds(self, artifact_store, session_factory) -> None:
        """The default wiring builds a runnable pipeline (stub-free construction)."""
        pipeline = build_default_research_pipeline(
            session_factory=session_factory,
            artifact_store=artifact_store,
            research_settings=ResearchSettings(max_sources_standard=3),
        )
        assert JobType.RESEARCH in pipeline.handlers

"""Research engine unit tests (stub providers — deterministic, no network).

Covers claim building, the stage flow, checkpoint resume, and the report
assembly. The production providers are tested separately (web provider unit
tests + the simulated integration tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from factory.config.research_config import ResearchSettings
from factory.jobs.service import JobService
from factory.jobs.types import JobStatus, JobType
from factory.providers.research.base import (
    ClaimVerifier,
    EvidenceExtractor,
    SourceProvider,
)
from factory.providers.research.types import (
    CollectedSource,
    VerificationResult,
)
from factory.research.engine import ResearchEngine
from factory.research.pipeline import ResearchPipeline
from factory.research.planner import DeterministicResearchPlanner
from factory.research.sources import InMemorySourceCache, make_candidate
from factory.schemas.artifacts import (
    ArtifactType,
    EvidenceItem,
    OpportunityItem,
    OpportunityList,
    ResearchReport,
)


def _opportunity_payload() -> dict[str, Any]:
    return OpportunityList(
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
                supporting_video_ids=["vid-comp-1"],
                confidence=0.8,
            )
        ],
    ).model_dump(mode="json")


class StubSourceProvider(SourceProvider):
    """In-memory SourceProvider with a fixed candidate set."""

    name = "stub-sources"

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages  # url → text
        self.discover_calls = 0
        self.collect_calls: list[str] = []

    def discover_sources(self, query, *, context=None, limit=10, source_types=None, job_id=None):
        self.discover_calls += 1
        candidates = []
        for index, url in enumerate(self.pages):
            candidates.append(
                make_candidate(
                    url,
                    title=f"Page {index}",
                    discovery_query=query,
                    relevance_score=1.0 - index * 0.1,
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    def collect_source(self, source, *, job_id=None):
        self.collect_calls.append(source.url)
        content = self.pages.get(source.url, "")
        return CollectedSource(
            candidate=source,
            content=content,
            content_type="text/plain",
            byte_size=len(content),
            content_fingerprint="f" * 64 if content else None,
            collection_status="collected" if content else "failed",
            collection_error=None if content else "not found",
            collected_at=datetime.now(UTC),
        )


class StubExtractor(EvidenceExtractor):
    name = "stub-extractor"

    def __init__(self) -> None:
        self.calls = 0

    def extract_evidence(self, document, *, plan=None, job_id=None):
        self.calls += 1
        if not document.content:
            return []
        return [
            EvidenceItem(
                evidence_id=f"ev-{document.candidate.url_fingerprint[:8]}-0",
                source_id=f"src-{document.candidate.url_fingerprint[:16]}",
                claim=document.content.strip(),
                passage=document.content.strip(),
                location="sentence 1",
                confidence=0.9,
                extracted_at=datetime.now(UTC),
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
                claim_id=f"cl-{index:04d}",
                verification_status="SUPPORTED",
                confidence=0.8,
                supporting_source_ids=[f"src-{s.candidate.url_fingerprint[:16]}" for s in sources],
                independent_source_count=len(sources),
                evidence_coverage=1.0,
            )
            for index, _claim in enumerate(claims)
        ]


@pytest.fixture
def opportunity_artifact(artifact_store) -> str:
    record = artifact_store.save(ArtifactType.OPPORTUNITY_LIST, "proj-1", _opportunity_payload())
    artifact_id: str = record.id
    return artifact_id


@pytest.fixture
def engine(artifact_store, session_factory) -> ResearchEngine:
    pages = {
        "https://a.example/x": "The Aqua Aqueduct was completed in 1312.",
        "https://b.example/y": "The Aqua Aqueduct was completed in 1312.",
    }
    provider = StubSourceProvider(pages)
    return ResearchEngine(
        planner=DeterministicResearchPlanner(),
        source_provider=provider,
        extractor=StubExtractor(),
        verifier=StubVerifier(),
        artifact_store=artifact_store,
        source_cache=InMemorySourceCache(),
        settings=ResearchSettings(max_sources_standard=10),
    )


@pytest.fixture
def job_service(projects) -> JobService:
    return JobService(projects, retry_base_seconds=1, retry_max_seconds=5)


@pytest.fixture
def pipeline(engine: ResearchEngine) -> ResearchPipeline:
    """Wrap the engine in the job-system pipeline (status transitions, retries)."""
    return ResearchPipeline(engine=engine)


class TestClaimBuilding:
    def test_grouping_by_subject_predicate(self) -> None:
        engine = ResearchEngine.__new__(ResearchEngine)  # call the pure helper
        evidence: list[dict[str, Any]] = [
            {"claim": "a", "subject": "x", "predicate": "p", "value": "1", "value_type": "date"},
            {"claim": "b", "subject": "x", "predicate": "p", "value": "1", "value_type": "date"},
            {"claim": "c", "subject": "x", "predicate": "p", "value": "2", "value_type": "date"},
            {"claim": "d", "subject": "y", "predicate": "q", "value": "9", "value_type": "number"},
            {"claim": "e", "subject": None, "predicate": None, "value": None, "value_type": None},
        ]
        claims = engine._build_claims(evidence)
        assert len(claims) == 3  # (x,p) merged, (y,q), unstructured
        merged = next(c for c in claims if c["subject"] == "x")
        assert len(merged["evidence"]) == 3
        assert merged["value"] == "1"  # modal value

    def test_claim_statement_generated(self) -> None:
        engine = ResearchEngine.__new__(ResearchEngine)
        claims = engine._build_claims(
            [{"claim": "a", "subject": "x", "predicate": "p", "value": "1", "value_type": "date"}]
        )
        assert claims[0]["statement"] == "The x p 1."


class TestEngineFlow:
    def test_full_run_produces_report(
        self, pipeline, job_service: JobService, opportunity_artifact: str, artifact_store
    ) -> None:
        job = job_service.enqueue(
            JobType.RESEARCH,
            project_id="proj-1",
            payload={
                "opportunity_list_artifact_id": opportunity_artifact,
                "opportunity_id": "opp-1",
            },
        )
        record = pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.SUCCEEDED
        _rec, payload = artifact_store.load(record.output_artifact_id)
        report = ResearchReport.model_validate(payload)
        assert report.opportunity_id == "opp-1"
        assert report.source_list
        assert report.verified_claims
        assert report.research_plan_id
        # Legacy fields populated:
        assert report.claims and report.sources

    def test_checkpoint_resume_after_extraction_failure(
        self,
        pipeline,
        engine: ResearchEngine,
        job_service: JobService,
        opportunity_artifact: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = engine._extractor.extract_evidence
        calls = {"count": 0}

        def flaky(document, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("extraction outage")
            return original(document, **kwargs)

        monkeypatch.setattr(engine._extractor, "extract_evidence", flaky)
        job = job_service.enqueue(
            JobType.RESEARCH,
            project_id="proj-1",
            payload={"opportunity_list_artifact_id": opportunity_artifact},
        )
        failed = pipeline.run_job(job_service, job.id)
        assert failed.status == JobStatus.PENDING
        assert failed.error is not None and failed.error.type == "RuntimeError"
        # Checkpoint captured the earlier stages:
        assert failed.checkpoint["stage"] in ("EVIDENCE_EXTRACTION",)
        assert failed.checkpoint["collected"]

        resumed = pipeline.run_job(job_service, job.id)
        assert resumed.status == JobStatus.SUCCEEDED
        assert resumed.output_artifact_id

    def test_unknown_opportunity_id_rejected(
        self, pipeline, job_service: JobService, opportunity_artifact: str
    ) -> None:
        job = job_service.enqueue(
            JobType.RESEARCH,
            project_id="proj-1",
            payload={
                "opportunity_list_artifact_id": opportunity_artifact,
                "opportunity_id": "does-not-exist",
            },
        )
        record = pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.PENDING
        assert record.error is not None
        assert "not found" in (record.error.message or "")

    def test_missing_opportunity_list_rejected(self, pipeline, job_service: JobService) -> None:
        job = job_service.enqueue(JobType.RESEARCH, project_id="proj-1", payload={})
        record = pipeline.run_job(job_service, job.id)
        assert record.status == JobStatus.PENDING
        assert record.error is not None

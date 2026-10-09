"""Phase 3A content-plane integration tests using only local artifacts."""

from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from factory.content.pipeline import build_default_content_pipeline
from factory.jobs.service import JobService
from factory.jobs.types import JobStatus, JobType
from factory.schemas.artifacts import (
    ArtifactType,
    EvidenceItem,
    OpportunityItem,
    OpportunityList,
    ResearchReport,
    SourceItem,
    VerifiedClaim,
    load_json_schema,
)
from factory.storage.models import Artifact

SCHEMAS_DIR = Path(__file__).resolve().parent.parent.parent / "schemas" / "artifacts"


def _inputs(artifact_store, *, report_opportunity_id: str = "opp-1") -> tuple[str, str]:
    opportunities = OpportunityList(
        opportunity_list_id="opportunities-1",
        project_id="proj-1",
        opportunities=[
            OpportunityItem(
                opportunity_id="opp-1",
                title="The Hidden Engineering of Ancient Aqueducts",
                topic="ancient aqueducts",
                score=80,
                rationale="An underserved explanatory angle.",
                audience_question="How did ancient aqueducts move water across difficult terrain?",
                content_gap="Explain the engineering trade-offs with source-backed examples.",
                recommended_angle=(
                    "Follow the engineering problem rather than retelling a competitor video."
                ),
            )
        ],
    )
    opportunity_record = artifact_store.save(ArtifactType.OPPORTUNITY_LIST, "proj-1", opportunities)
    report = ResearchReport(
        research_report_id="research-job-1",
        project_id="proj-1",
        opportunity_id=report_opportunity_id,
        topic="ancient aqueducts",
        summary="Research report with one supported and one unresolved claim.",
        sources=[
            {
                "source_id": "src-1",
                "url": "https://example.org/history",
                "title": "History reference",
            }
        ],
        source_list=[
            SourceItem(
                source_id="src-1",
                url="https://example.org/history",
                canonical_url="https://example.org/history",
                url_fingerprint="a" * 64,
                title="History reference",
                collection_status="collected",
            )
        ],
        evidence=[
            EvidenceItem(
                evidence_id="ev-1",
                source_id="src-1",
                claim="A supported aqueduct engineering fact.",
                passage="A bounded passage supporting the claim.",
                confidence=0.9,
            )
        ],
        verified_claims=[
            VerifiedClaim(
                claim_id="claim-supported",
                statement="The aqueduct used a carefully graded channel.",
                verification_status="SUPPORTED",
                confidence=0.9,
                # Phase 2 currently populates this field with source ids; Phase 3A
                # must normalize them to concrete evidence ids.
                evidence_refs=["src-1"],
                supporting_source_ids=["src-1"],
            ),
            VerifiedClaim(
                claim_id="claim-uncertain",
                statement="An uncertain claim that must not be narrated as fact.",
                verification_status="CONTESTED",
                confidence=0.4,
                evidence_refs=[],
            ),
        ],
        unresolved_questions=["The exact construction date remains unresolved."],
    )
    report_record = artifact_store.save(ArtifactType.RESEARCH_REPORT, "proj-1", report)
    return opportunity_record.id, report_record.id


def test_content_job_creates_traceable_brief_and_narrative_outline(
    artifact_store, projects
) -> None:
    opportunity_id, report_id = _inputs(artifact_store)
    pipeline = build_default_content_pipeline(artifact_store=artifact_store)
    service = JobService(projects)
    job = service.enqueue(
        JobType.CONTENT_BRIEF,
        project_id="proj-1",
        input_artifact_id=opportunity_id,
        payload={
            "opportunity_list_artifact_id": opportunity_id,
            "opportunity_id": "opp-1",
            "research_report_artifact_id": report_id,
        },
        idempotency_key="phase-3a-content-1",
    )

    result = pipeline.run_job(service, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert result.output_artifact_id
    _outline_record, outline_payload = artifact_store.load(result.output_artifact_id)
    assert _outline_record.type == ArtifactType.NARRATIVE_OUTLINE.value
    Draft202012Validator(load_json_schema(ArtifactType.NARRATIVE_OUTLINE, SCHEMAS_DIR)).validate(
        outline_payload
    )
    assert [beat["beat_type"] for beat in outline_payload["beats"]] == [
        "hook",
        "setup",
        "escalation",
        "turning_point",
        "resolution",
        "final_insight",
    ]
    assert report_id in outline_payload["source_artifact_ids"]

    _brief_record, brief_payload = artifact_store.load_latest(
        "proj-1", ArtifactType.CONTENT_BRIEF, lineage_key=job.id
    )
    Draft202012Validator(load_json_schema(ArtifactType.CONTENT_BRIEF, SCHEMAS_DIR)).validate(
        brief_payload
    )
    assert brief_payload["opportunity_id"] == "opp-1"
    assert brief_payload["research_report_id"] == report_id
    assert brief_payload["evidence_backed_points"][0]["claim_refs"] == ["claim-supported"]
    assert brief_payload["evidence_backed_points"][0]["evidence_refs"] == ["ev-1"]
    assert brief_payload["evidence_backed_points"][0]["source_urls"] == [
        "https://example.org/history"
    ]
    assert (
        "An uncertain claim that must not be narrated as fact."
        in brief_payload["unresolved_claims_to_avoid"]
    )
    assert (
        "The exact construction date remains unresolved."
        in brief_payload["unresolved_claims_to_avoid"]
    )


def test_content_job_rejects_report_for_another_opportunity(artifact_store, projects) -> None:
    opportunity_id, report_id = _inputs(artifact_store, report_opportunity_id="different-opp")
    pipeline = build_default_content_pipeline(artifact_store=artifact_store)
    service = JobService(projects)
    job = service.enqueue(
        JobType.CONTENT_BRIEF,
        project_id="proj-1",
        input_artifact_id=opportunity_id,
        payload={
            "opportunity_list_artifact_id": opportunity_id,
            "opportunity_id": "opp-1",
            "research_report_artifact_id": report_id,
        },
    )

    result = pipeline.run_job(service, job.id)

    # The shared job runner schedules retryable failures before exhausting retries.
    assert result.status == JobStatus.PENDING
    assert result.error is not None
    assert result.output_artifact_id is None


def test_content_job_resumes_after_brief_checkpoint_without_duplicate_brief(
    artifact_store, projects, monkeypatch
) -> None:
    opportunity_id, report_id = _inputs(artifact_store)
    pipeline = build_default_content_pipeline(artifact_store=artifact_store)
    service = JobService(projects, retry_base_seconds=0, retry_max_seconds=0)
    job = service.enqueue(
        JobType.CONTENT_BRIEF,
        project_id="proj-1",
        input_artifact_id=opportunity_id,
        payload={
            "opportunity_list_artifact_id": opportunity_id,
            "opportunity_id": "opp-1",
            "research_report_artifact_id": report_id,
        },
    )
    original_save = artifact_store.save
    fail_once = {"value": True}

    def flaky_save(artifact_type, project_id, payload, **kwargs):
        if ArtifactType(artifact_type) == ArtifactType.NARRATIVE_OUTLINE and fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("simulated transient outline-save failure")
        return original_save(artifact_type, project_id, payload, **kwargs)

    monkeypatch.setattr(artifact_store, "save", flaky_save)
    first = pipeline.run_job(service, job.id)
    assert first.status == JobStatus.PENDING
    brief_artifact_id = first.checkpoint["brief_artifact_id"]

    second = pipeline.run_job(service, job.id)
    assert second.status == JobStatus.SUCCEEDED
    assert second.output_artifact_id
    assert second.checkpoint["brief_artifact_id"] == brief_artifact_id

    with projects() as session:
        count = session.scalar(
            select(func.count())
            .select_from(Artifact)
            .where(Artifact.type == ArtifactType.CONTENT_BRIEF.value, Artifact.job_id == job.id)
        )
    assert count == 1

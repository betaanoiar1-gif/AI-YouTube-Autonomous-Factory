"""Deterministic Phase 3A content brief and narrative-outline engine.

Only verified research claims become factual key points. Unverified and
contested claims are explicitly excluded from factual narration.
"""
from __future__ import annotations

import hashlib
from typing import Any

from factory.jobs.runner import JobContext
from factory.jobs.types import JobRecord
from factory.schemas.artifacts import (
    ArtifactType,
    ContentBrief,
    EvidenceBackedPoint,
    NarrativeBeat,
    NarrativeOutline,
    OpportunityList,
    ResearchReport,
    validate_artifact_payload,
)
from factory.storage.artifacts import ArtifactStore

_SUPPORTED = {"SUPPORTED", "MULTI_SOURCE_SUPPORTED"}


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


class ContentEngine:
    """Create a traceable brief and outline from opportunity + research artifacts."""

    def __init__(self, *, artifact_store: ArtifactStore) -> None:
        self._artifacts = artifact_store

    def run(self, job: JobRecord, context: JobContext) -> str:
        """Save the content brief and outline; return the outline artifact id."""
        project_id = job.project_id or ""
        if not project_id:
            raise ValueError("content brief job requires a project_id")

        payload = job.payload or {}
        checkpoint: dict[str, Any] = dict(context.checkpoint or {})
        opportunity_list_id = payload.get("opportunity_list_artifact_id") or job.input_artifact_id
        if not isinstance(opportunity_list_id, str) or not opportunity_list_id:
            raise ValueError("content brief job requires opportunity_list_artifact_id")

        _, opportunity_payload = self._artifacts.load(opportunity_list_id)
        normalized_opportunities = validate_artifact_payload(
            ArtifactType.OPPORTUNITY_LIST, opportunity_payload
        )
        opportunity_list = OpportunityList.model_validate(normalized_opportunities)
        requested_id = payload.get("opportunity_id")
        if requested_id:
            opportunity = next(
                (item for item in opportunity_list.opportunities
                 if item.opportunity_id == requested_id),
                None,
            )
        else:
            opportunity = (
                opportunity_list.opportunities[0] if opportunity_list.opportunities else None
            )
        if opportunity is None:
            raise ValueError("selected opportunity was not found in opportunity_list artifact")

        requested_report_id = payload.get("research_report_artifact_id")
        if isinstance(requested_report_id, str) and requested_report_id:
            report_record, report_payload = self._artifacts.load(requested_report_id)
        else:
            report_record, report_payload = self._artifacts.load_latest(
                project_id, ArtifactType.RESEARCH_REPORT
            )
        normalized_report = validate_artifact_payload(
            ArtifactType.RESEARCH_REPORT, report_payload
        )
        report = ResearchReport.model_validate(normalized_report)
        if report.opportunity_id != opportunity.opportunity_id:
            raise ValueError("research report does not belong to the selected opportunity")
        if report.project_id and report.project_id != project_id:
            raise ValueError("research report belongs to a different project")

        brief_id = _stable_id(
            "brief", project_id, opportunity.opportunity_id, report.research_report_id
        )
        outline_id = _stable_id(
            "outline", project_id, opportunity.opportunity_id, report.research_report_id
        )
        source_artifact_ids = list(
            dict.fromkeys([opportunity_list_id, report_record.id])
        )
        points = self._evidence_backed_points(report)
        unresolved = self._unresolved_claims(report)

        audience = payload.get("target_audience") or (
            "Curious viewers seeking a clear, evidence-led explanation"
        )
        question = (
            opportunity.audience_question
            or report.research_question
            or f"What explains {opportunity.topic}?"
        )
        angle = (
            opportunity.recommended_angle
            or opportunity.novelty_rationale
            or opportunity.content_gap
            or opportunity.rationale
        )
        duration = payload.get("estimated_duration_seconds", 600)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
            raise ValueError("estimated_duration_seconds must be a non-negative integer")

        brief = ContentBrief(
            brief_id=brief_id,
            opportunity_id=opportunity.opportunity_id,
            project_id=project_id,
            research_report_id=report_record.id,
            title=opportunity.title,
            angle=angle,
            target_audience=audience,
            central_promise=question,
            audience_question=question,
            originality_angle=angle,
            key_points=[point.claim for point in points],
            evidence_backed_points=points,
            content_gaps_addressed=[
                value
                for value in [opportunity.content_gap, opportunity.novelty_rationale]
                if value
            ],
            unresolved_claims_to_avoid=unresolved,
            intended_tone=str(payload.get("intended_tone") or "cinematic documentary"),
            content_constraints=[
                "Use only supported claims as factual statements.",
                "Preserve source and evidence references for factual claims.",
                "Do not copy competitor titles, descriptions, or transcripts.",
                "Present unresolved contradictions as unresolved; do not guess.",
            ],
            source_artifact_ids=source_artifact_ids,
            estimated_duration_seconds=duration,
        )

        brief_artifact_id = checkpoint.get("brief_artifact_id")
        if not brief_artifact_id:
            brief_record = self._artifacts.save(
                ArtifactType.CONTENT_BRIEF,
                project_id,
                brief,
                job_id=job.id,
                metadata={
                    "job_type": job.type.value,
                    "opportunity_id": opportunity.opportunity_id,
                    "research_report_artifact_id": report_record.id,
                },
            )
            brief_artifact_id = brief_record.id
            context.set_progress(45)
            context.save_checkpoint(
                {
                    "stage": "OUTLINE",
                    "brief_artifact_id": brief_artifact_id,
                    "brief_id": brief_id,
                    "outline_id": outline_id,
                    "opportunity_list_artifact_id": opportunity_list_id,
                    "research_report_artifact_id": report_record.id,
                }
            )

        outline = NarrativeOutline(
            outline_id=outline_id,
            brief_id=brief_id,
            opportunity_id=opportunity.opportunity_id,
            project_id=project_id,
            title=opportunity.title,
            beats=self._narrative_beats(outline_id, question, points),
            estimated_duration_seconds=duration,
            source_artifact_ids=[*source_artifact_ids, str(brief_artifact_id)],
        )
        outline_artifact_id = checkpoint.get("outline_artifact_id")
        if not outline_artifact_id:
            outline_record = self._artifacts.save(
                ArtifactType.NARRATIVE_OUTLINE,
                project_id,
                outline,
                job_id=job.id,
                metadata={
                    "job_type": job.type.value,
                    "opportunity_id": opportunity.opportunity_id,
                    "brief_artifact_id": str(brief_artifact_id),
                },
            )
            outline_artifact_id = outline_record.id
            context.save_checkpoint(
                {
                    "stage": "DONE",
                    "brief_artifact_id": str(brief_artifact_id),
                    "outline_artifact_id": outline_artifact_id,
                    "brief_id": brief_id,
                    "outline_id": outline_id,
                    "opportunity_list_artifact_id": opportunity_list_id,
                    "research_report_artifact_id": report_record.id,
                }
            )

        context.set_progress(100)
        return str(outline_artifact_id)

    @staticmethod
    def _evidence_backed_points(report: ResearchReport) -> list[EvidenceBackedPoint]:
        source_urls = {source.source_id: source.url for source in report.source_list}
        source_urls.update(
            {source.source_id: source.url for source in report.sources if source.source_id}
        )
        evidence_by_id = {item.evidence_id: item for item in report.evidence}
        evidence_by_source: dict[str, list[Any]] = {}
        for item in report.evidence:
            evidence_by_source.setdefault(item.source_id, []).append(item)
        points: list[EvidenceBackedPoint] = []
        for claim in report.verified_claims:
            if claim.verification_status.upper() not in _SUPPORTED:
                continue
            linked_evidence: dict[str, Any] = {}
            for ref in claim.evidence_refs:
                if ref in evidence_by_id:
                    linked_evidence[ref] = evidence_by_id[ref]
                else:
                    # Phase 2 currently emits source ids in VerifiedClaim.evidence_refs;
                    # normalize those legacy references back to concrete evidence ids.
                    for item in evidence_by_source.get(ref, []):
                        linked_evidence[item.evidence_id] = item
            evidence_refs = list(linked_evidence)
            source_ids = list(
                dict.fromkeys(
                    [
                        *claim.supporting_source_ids,
                        *(item.source_id for item in linked_evidence.values()),
                    ]
                )
            )
            urls = list(dict.fromkeys(url for sid in source_ids if (url := source_urls.get(sid))))
            if not evidence_refs and not any(sid in source_urls for sid in source_ids):
                continue
            points.append(
                EvidenceBackedPoint(
                    claim=claim.statement,
                    claim_refs=[claim.claim_id],
                    evidence_refs=evidence_refs,
                    source_ids=source_ids,
                    source_urls=urls,
                )
            )
        return points

    @staticmethod
    def _unresolved_claims(report: ResearchReport) -> list[str]:
        unresolved = list(report.unresolved_questions)
        for claim in [*report.contested_claims, *report.verified_claims]:
            if claim.verification_status.upper() not in _SUPPORTED:
                unresolved.append(claim.statement)
        unresolved.extend(item.description for item in report.contradiction_details)
        return list(dict.fromkeys(unresolved))

    @staticmethod
    def _narrative_beats(
        outline_id: str, question: str, points: list[EvidenceBackedPoint]
    ) -> list[NarrativeBeat]:
        def claim_refs(start: int, end: int | None = None) -> list[str]:
            selected = points[start:end]
            return [point.claim_refs[0] for point in selected]

        def evidence_refs(start: int, end: int | None = None) -> list[str]:
            selected = points[start:end]
            return list(dict.fromkeys(ref for point in selected for ref in point.evidence_refs))

        return [
            NarrativeBeat(
                beat_id=f"{outline_id}-hook",
                index=0,
                beat_type="hook",
                title="The central puzzle",
                purpose=question,
            ),
            NarrativeBeat(
                beat_id=f"{outline_id}-setup",
                index=1,
                beat_type="setup",
                title="What is known",
                purpose="Establish only the context supported by the research.",
                claim_refs=claim_refs(0, 2),
                evidence_refs=evidence_refs(0, 2),
            ),
            NarrativeBeat(
                beat_id=f"{outline_id}-escalation",
                index=2,
                beat_type="escalation",
                title="Follow the evidence",
                purpose="Build the explanation from the strongest supported points.",
                claim_refs=claim_refs(2, 5),
                evidence_refs=evidence_refs(2, 5),
            ),
            NarrativeBeat(
                beat_id=f"{outline_id}-turn",
                index=3,
                beat_type="turning_point",
                title="The crucial finding",
                purpose=(
                    "Reveal the most consequential supported finding without overstating certainty."
                ),
                claim_refs=claim_refs(0, 1),
                evidence_refs=evidence_refs(0, 1),
            ),
            NarrativeBeat(
                beat_id=f"{outline_id}-resolution",
                index=4,
                beat_type="resolution",
                title="What the evidence can establish",
                purpose=(
                    "Resolve the audience question as far as the sources allow; "
                    "state remaining uncertainty."
                ),
                claim_refs=claim_refs(0),
                evidence_refs=evidence_refs(0),
            ),
            NarrativeBeat(
                beat_id=f"{outline_id}-insight",
                index=5,
                beat_type="final_insight",
                title="Why it matters",
                purpose=f"Return to the original question: {question}",
            ),
        ]

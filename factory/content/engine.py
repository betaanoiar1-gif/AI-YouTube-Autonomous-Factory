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
        project_id = job.project_id or ""
        if not project_id:
            raise ValueError("content brief job requires a project_id")
        payload = job.payload or {}
        checkpoint: dict[str, Any] = dict(context.checkpoint or {})
        opportunity_list_id = payload.get("opportunity_list_artifact_id") or job.input_artifact_id
        if not isinstance(opportunity_list_id, str) or not opportunity_list_id:
            raise ValueError("content brief job requires opportunity_list_artifact_id")
        _op_record, opportunity_payload = self._artifacts.load(opportunity_list_id)
        opportunity_list = OpportunityList.model_validate(
            validate_artifact_payload(ArtifactType.OPPORTUNITY_LIST, opportunity_payload)
        )
        requested_opportunity_id = payload.get("opportunity_id")
        opportunity = next(
            (
                item
                for item in opportunity_list.opportunities
                if item.opportunity_id == requested_opportunity_id
            ),
            None,
        ) if requested_opportunity_id else (opportunity_list.opportunities[0] if opportunity_list.opportunities else None)
        if opportunity is None:
            raise ValueError("selected opportunity was not found in opportunity_list artifact")

        report_id = payload.get("research_report_artifact_id")
        if isinstance(report_id, str) and report_id:
            _report_record, report_payload = self._artifacts.load(report_id)
        else:
            _report_record, report_payload = self._artifacts.load_latest(
                project_id, ArtifactType.RESEARCH_REPORT
            )
        report = ResearchReport.model_validate(
            validate_artifact_payload(ArtifactType.RESEARCH_REPORT, report_payload)
        )
        if report.opportunity_id != opportunity.opportunity_id:
            raise ValueError("research report does not belong to the selected opportunity")
        if report.project_id and report.project_id != project_id:
            raise ValueError("research report belongs to a different project")

        report_id = report.research_report_id
        brief_id = _stable_id("brief", project_id, opportunity.opportunity_id, report_id)
        outline_id = _stable_id("outline", project_id, opportunity.opportunity_id, report_id)
        source_urls = {item.source_id: item.url for item in report.source_list}
        source_urls.update({item.source_id: item.url for item in report.sources if item.source_id})
        evidence_by_id = {item.evidence_id: item for item in report.evidence}
        supported = [claim for claim in report.verified_claims if claim.verification_status.upper() in _SUPPORTED]
        points: list[EvidenceBackedPoint] = []
        for claim in supported:
            evidence_refs = [ref for ref in claim.evidence_refs if ref in evidence_by_id]
            source_ids = list(dict.fromkeys(
                [*claim.supporting_source_ids, *(evidence_by_id[ref].source_id for ref in evidence_refs)]
            ))
            urls = list(dict.fromkeys(source_urls[sid] for sid in source_ids if sid in source_urls))
            points.append(EvidenceBackedPoint(
                claim=claim.statement,
                claim_refs=[claim.claim_id],
                evidence_refs=evidence_refs,
                source_ids=source_ids,
                source_urls=urls,
            ))

        unresolved = list(report.unresolved_questions)
        for claim in [*report.contested_claims, *report.verified_claims]:
            if claim.verification_status.upper() not in _SUPPORTED:
                unresolved.append(claim.statement)
        unresolved.extend(item.description for item in report.contradiction_details)
        unresolved = list(dict.fromkeys(unresolved))

        audience = payload.get("target_audience") or "Curious viewers seeking a clear, evidence-led explanation"
        question = opportunity.audience_question or report.research_question or f"What explains {opportunity.topic}?"
        angle = opportunity.recommended_angle or opportunity.novelty_rationale or opportunity.content_gap or opportunity.rationale
        title = opportunity.title
        duration = payload.get("estimated_duration_seconds", 600)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
            raise ValueError("estimated_duration_seconds must be a non-negative integer")
        source_artifact_ids = list(dict.fromkeys([
            opportunity_list_id,
            str(payload.get("research_report_artifact_id") or _report_record.id),
        ]))
        brief = ContentBrief(
            brief_id=brief_id,
            opportunity_id=opportunity.opportunity_id,
            project_id=project_id,
            research_report_id=str(payload.get("research_report_artifact_id") or _report_record.id),
            title=title,
            angle=angle,
            target_audience=audience,
            central_promise=question,
            audience_question=question,
            originality_angle=angle,
            key_points=[point.claim for point in points],
            evidence_backed_points=points,
            content_gaps_addressed=[v for v in [opportunity.content_gap, opportunity.novelty_rationale] if v],
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
                ArtifactType.CONTENT_BRIEF, project_id, brief, job_id=job.id,
                metadata={"job_type": job.type.value, "opportunity_id": opportunity.opportunity_id,
                          "research_report_artifact_id": source_artifact_ids[1]},
            )
            brief_artifact_id = brief_record.id
            context.set_progress(45)
            context.save_checkpoint({
                "stage": "OUTLINE", "brief_artifact_id": brief_artifact_id,
                "brief_id": brief_id, "outline_id": outline_id,
                "opportunity_list_artifact_id": opportunity_list_id,
                "research_report_artifact_id": source_artifact_ids[1],
            })

        beats = [
            NarrativeBeat(beat_id=f"{outline_id}-hook", index=0, beat_type="hook",
                          title="The central puzzle", purpose=question),
            NarrativeBeat(beat_id=f"{outline_id}-setup", index=1, beat_type="setup",
                          title="What is known", purpose="Establish only the context supported by the research.",
                          claim_refs=[p.claim_refs[0] for p in points[:2]],
                          evidence_refs=[e for p in points[:2] for e in p.evidence_refs]),
            NarrativeBeat(beat_id=f"{outline_id}-escalation", index=2, beat_type="escalation",
                          title="Follow the evidence", purpose="Build the explanation from the strongest supported points.",
                          claim_refs=[p.claim_refs[0] for p in points[2:5]],
                          evidence_refs=[e for p in points[2:5] for e in p.evidence_refs]),
            NarrativeBeat(beat_id=f"{outline_id}-turn", index=3, beat_type="turning_point",
                          title="The crucial finding", purpose="Reveal the most consequential supported finding without overstating certainty.",
                          claim_refs=[points[0].claim_refs[0]] if points else [],
                          evidence_refs=points[0].evidence_refs if points else []),
            NarrativeBeat(beat_id=f"{outline_id}-resolution", index=4, beat_type="resolution",
                          title="What the evidence can establish", purpose="Resolve the audience question as far as the sources allow; state remaining uncertainty.",
                          claim_refs=[p.claim_refs[0] for p in points],
                          evidence_refs=[e for p in points for e in p.evidence_refs]),
            NarrativeBeat(beat_id=f"{outline_id}-insight", index=5, beat_type="final_insight",
                          title="Why it matters", purpose=f"Return to the original question: {question}"),
        ]
        outline = NarrativeOutline(
            outline_id=outline_id, brief_id=brief_id, opportunity_id=opportunity.opportunity_id,
            project_id=project_id, title=title, beats=beats,
            estimated_duration_seconds=duration, source_artifact_ids=[*source_artifact_ids, str(brief_artifact_id)],
        )
        outline_artifact_id = checkpoint.get("outline_artifact_id")
        if not outline_artifact_id:
            outline_record = self._artifacts.save(
                ArtifactType.NARRATIVE_OUTLINE, project_id, outline, job_id=job.id,
                metadata={"job_type": job.type.value, "opportunity_id": opportunity.opportunity_id,
                          "brief_artifact_id": str(brief_artifact_id)},
            )
            outline_artifact_id = outline_record.id
        context.set_progress(100)
        context.save_checkpoint({
            "stage": "DONE", "brief_artifact_id": str(brief_artifact_id),
            "outline_artifact_id": str(outline_artifact_id), "brief_id": brief_id, "outline_id": outline_id,
            "opportunity_list_artifact_id": opportunity_list_id,
            "research_report_artifact_id": source_artifact_ids[1],
        })
        return str(outline_artifact_id)

"""Research engine (RESEARCH job) — the Phase 2 pipeline.

Stages (each checkpointed; a failed later stage never repeats completed
earlier stages):

    PLAN → SOURCE_DISCOVERY → SOURCE_COLLECTION → EVIDENCE_EXTRACTION
         → CLAIM_BUILDING → VERIFICATION → CONTRADICTION_ANALYSIS → REPORT

Input: an `opportunity_list` artifact + a selected opportunity id +
research depth/language configuration. Output: a `research_report` artifact
(with a `research_plan` artifact as the planning-stage output).

Originality boundary: the research is derived from the opportunity (topic,
audience question, market signals) and independently collected sources — never
from competitor video content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from factory.config.research_config import ResearchSettings, get_research_settings
from factory.jobs.runner import JobContext
from factory.jobs.types import JobRecord
from factory.providers.research.base import (
    ClaimVerifier,
    EvidenceExtractor,
    ResearchProvider,
    SourceProvider,
)
from factory.providers.research.types import ClaimDraft, CollectedSource
from factory.schemas.artifacts import (
    ArtifactType,
    EvidenceItem,
    OpportunityItem,
    OpportunityList,
    ResearchPlan,
    ResearchReport,
    SourceItem,
    VerifiedClaim,
    validate_artifact_payload,
)
from factory.storage.artifacts import ArtifactStore

#: Pipeline stages.
STAGE_PLAN = "PLAN"
STAGE_SOURCE_DISCOVERY = "SOURCE_DISCOVERY"
STAGE_SOURCE_COLLECTION = "SOURCE_COLLECTION"
STAGE_EVIDENCE_EXTRACTION = "EVIDENCE_EXTRACTION"
STAGE_CLAIM_BUILDING = "CLAIM_BUILDING"
STAGE_VERIFICATION = "VERIFICATION"
STAGE_CONTRADICTION_ANALYSIS = "CONTRADICTION_ANALYSIS"
STAGE_REPORT = "REPORT"

_STAGES = (
    STAGE_PLAN,
    STAGE_SOURCE_DISCOVERY,
    STAGE_SOURCE_COLLECTION,
    STAGE_EVIDENCE_EXTRACTION,
    STAGE_CLAIM_BUILDING,
    STAGE_VERIFICATION,
    STAGE_CONTRADICTION_ANALYSIS,
    STAGE_REPORT,
)


class ResearchJobConfig:
    """Validated RESEARCH job input (from the job payload)."""

    def __init__(self, payload: dict[str, Any], *, job: JobRecord) -> None:
        self.opportunity_id = payload.get("opportunity_id")
        if not isinstance(self.opportunity_id, str) or not self.opportunity_id:
            self.opportunity_id = None
        depth = payload.get("depth", "standard")
        self.depth = depth if depth in ("standard", "deep") else "standard"
        language = payload.get("language")
        self.language = language if isinstance(language, str) else None
        audience = payload.get("target_audience")
        self.target_audience = audience if isinstance(audience, str) else None
        self.opportunity_list_artifact_id = payload.get("opportunity_list_artifact_id")


class ResearchEngine:
    """Runs the research pipeline through the provider contracts."""

    def __init__(
        self,
        *,
        planner: ResearchProvider,
        source_provider: SourceProvider,
        extractor: EvidenceExtractor,
        verifier: ClaimVerifier,
        artifact_store: ArtifactStore,
        source_cache: Any | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        self._planner = planner
        self._source_provider = source_provider
        self._extractor = extractor
        self._verifier = verifier
        self._artifact_store = artifact_store
        self._source_cache = source_cache
        self._settings = settings or get_research_settings()

    def run(self, job: JobRecord, context: JobContext) -> str:
        """Run the research job; returns the research_report artifact id."""
        config = ResearchJobConfig(job.payload or {}, job=job)
        project_id = job.project_id or ""
        if not project_id:
            raise ValueError("research job requires a project_id")

        checkpoint: dict[str, Any] = dict(context.checkpoint or {})
        stage = checkpoint.get("stage", STAGE_PLAN)
        if stage not in _STAGES:
            stage = STAGE_PLAN
        started_at = checkpoint.get("started_at") or datetime.now(UTC).isoformat()

        opportunity = self._load_opportunity(job, config)
        plan: ResearchPlan | None = None
        plan_artifact_id: str | None = checkpoint.get("plan_artifact_id")
        candidates: list[dict[str, Any]] = list(checkpoint.get("candidates", []))
        collected: list[dict[str, Any]] = list(checkpoint.get("collected", []))
        evidence: list[dict[str, Any]] = list(checkpoint.get("evidence", []))
        claims: list[dict[str, Any]] = list(checkpoint.get("claims", []))
        verified: list[dict[str, Any]] = list(checkpoint.get("verified", []))
        contradictions: list[dict[str, Any]] = list(checkpoint.get("contradictions", []))

        # --- PLAN ---
        if stage == STAGE_PLAN:
            plan = self._planner.plan_research(
                opportunity,
                project_id=project_id,
                depth=config.depth,
                language=config.language,
                target_audience=config.target_audience,
                job_id=job.id,
            )
            record = self._artifact_store.save(
                ArtifactType.RESEARCH_PLAN,
                project_id,
                plan.model_dump(mode="json"),
                job_id=job.id,
                metadata={"job_type": job.type.value, "opportunity_id": opportunity.opportunity_id},
            )
            plan_artifact_id = record.id
            context.set_progress(12)
            context.save_checkpoint(
                {
                    "stage": STAGE_SOURCE_DISCOVERY,
                    "started_at": started_at,
                    "plan": plan.model_dump(mode="json"),
                    "plan_artifact_id": plan_artifact_id,
                }
            )
            stage = STAGE_SOURCE_DISCOVERY
            checkpoint = {
                "stage": stage,
                "started_at": started_at,
                "plan": plan.model_dump(mode="json"),
                "plan_artifact_id": plan_artifact_id,
            }
        else:
            plan = ResearchPlan.model_validate(checkpoint["plan"])

        # --- SOURCE_DISCOVERY ---
        if stage == STAGE_SOURCE_DISCOVERY:
            limit = self._settings.max_sources_for_depth(config.depth)
            queries = self._discovery_queries(plan, opportunity)
            seen_fingerprints: set[str] = set()
            for query in queries:
                if len(candidates) >= limit:
                    break
                found = self._source_provider.discover_sources(
                    query,
                    context=plan.central_question,
                    limit=max(1, limit - len(candidates)),
                    job_id=job.id,
                )
                for candidate in found:
                    if candidate.url_fingerprint in seen_fingerprints:
                        continue
                    seen_fingerprints.add(candidate.url_fingerprint)
                    candidates.append(candidate.model_dump(mode="json"))
                    if len(candidates) >= limit:
                        break
            context.set_progress(25)
            context.save_checkpoint(
                {
                    "stage": STAGE_SOURCE_COLLECTION,
                    "started_at": started_at,
                    "plan": plan.model_dump(mode="json"),
                    "plan_artifact_id": plan_artifact_id,
                    "candidates": candidates,
                }
            )
            stage = STAGE_SOURCE_COLLECTION
            checkpoint = {
                "stage": stage,
                "started_at": started_at,
                "plan": plan.model_dump(mode="json"),
                "plan_artifact_id": plan_artifact_id,
                "candidates": candidates,
            }

        # --- SOURCE_COLLECTION ---
        if stage == STAGE_SOURCE_COLLECTION:
            from factory.providers.research.types import SourceCandidate

            pending = [
                SourceCandidate.model_validate(c)
                for c in candidates
                if c["url_fingerprint"] not in {col["url_fingerprint"] for col in collected}
            ]
            total = len(candidates)
            for candidate in pending:
                source = self._source_provider.collect_source(candidate, job_id=job.id)
                # The engine owns cross-job caching: any provider's collected
                # sources are deduplicated here (checkpoints never carry content).
                if self._source_cache is not None:
                    self._source_cache.put(source)
                collected.append(self._collected_to_item(source))
                context.set_progress(25 + int((len(collected) / max(total, 1)) * 25))
                context.save_checkpoint(
                    {
                        "stage": STAGE_SOURCE_COLLECTION,
                        "started_at": started_at,
                        "plan": plan.model_dump(mode="json"),
                        "plan_artifact_id": plan_artifact_id,
                        "candidates": candidates,
                        "collected": collected,
                    }
                )
            stage = STAGE_EVIDENCE_EXTRACTION
            checkpoint = {
                "stage": stage,
                "started_at": started_at,
                "plan": plan.model_dump(mode="json"),
                "plan_artifact_id": plan_artifact_id,
                "candidates": candidates,
                "collected": collected,
            }
            context.save_checkpoint(checkpoint)

        # --- EVIDENCE_EXTRACTION ---
        if stage == STAGE_EVIDENCE_EXTRACTION:
            collected_sources = self._load_collected_sources(collected)
            done_ids = {e["source_id"] for e in evidence}
            for source in collected_sources:
                source_id = f"src-{source.candidate.url_fingerprint[:16]}"
                if source_id in done_ids:
                    continue  # already extracted (resume)
                if not source.content:
                    continue  # no cached content → no evidence (never invented)
                items = self._extractor.extract_evidence(source, plan=plan, job_id=job.id)
                for item in items:
                    evidence.append(item.model_dump(mode="json"))
                done_ids.add(source_id)
                context.set_progress(
                    50 + int((len(done_ids) / max(len(collected_sources), 1)) * 15)
                )
                context.save_checkpoint(
                    {
                        "stage": STAGE_EVIDENCE_EXTRACTION,
                        "started_at": started_at,
                        "plan": plan.model_dump(mode="json"),
                        "plan_artifact_id": plan_artifact_id,
                        "candidates": candidates,
                        "collected": collected,
                        "evidence": evidence,
                    }
                )
            stage = STAGE_CLAIM_BUILDING
            checkpoint = {
                "stage": stage,
                "started_at": started_at,
                "plan": plan.model_dump(mode="json"),
                "plan_artifact_id": plan_artifact_id,
                "candidates": candidates,
                "collected": collected,
                "evidence": evidence,
            }
            context.save_checkpoint(checkpoint)

        # --- CLAIM_BUILDING ---
        if stage == STAGE_CLAIM_BUILDING:
            claims = self._build_claims(evidence)
            context.set_progress(70)
            context.save_checkpoint(
                {
                    "stage": STAGE_VERIFICATION,
                    "started_at": started_at,
                    "plan": plan.model_dump(mode="json"),
                    "plan_artifact_id": plan_artifact_id,
                    "candidates": candidates,
                    "collected": collected,
                    "evidence": evidence,
                    "claims": claims,
                }
            )
            stage = STAGE_VERIFICATION
            checkpoint = {
                "stage": stage,
                "started_at": started_at,
                "plan": plan.model_dump(mode="json"),
                "plan_artifact_id": plan_artifact_id,
                "candidates": candidates,
                "collected": collected,
                "evidence": evidence,
                "claims": claims,
            }

        # --- VERIFICATION ---
        if stage == STAGE_VERIFICATION:
            drafts = [ClaimDraft.model_validate(c) for c in claims]
            collected_sources = self._load_collected_sources(collected)
            results = self._verifier.verify_claims(drafts, sources=collected_sources, job_id=job.id)
            verified = [
                self._verified_payload(draft, result)
                for draft, result in zip(drafts, results, strict=True)
            ]
            context.set_progress(82)
            context.save_checkpoint(
                {
                    "stage": STAGE_CONTRADICTION_ANALYSIS,
                    "started_at": started_at,
                    "plan": plan.model_dump(mode="json"),
                    "plan_artifact_id": plan_artifact_id,
                    "candidates": candidates,
                    "collected": collected,
                    "evidence": evidence,
                    "claims": claims,
                    "verified": verified,
                }
            )
            stage = STAGE_CONTRADICTION_ANALYSIS
            checkpoint = {
                "stage": stage,
                "started_at": started_at,
                "plan": plan.model_dump(mode="json"),
                "plan_artifact_id": plan_artifact_id,
                "candidates": candidates,
                "collected": collected,
                "evidence": evidence,
                "claims": claims,
                "verified": verified,
            }

        # --- CONTRADICTION_ANALYSIS ---
        if stage == STAGE_CONTRADICTION_ANALYSIS:
            contradictions = self._collect_contradictions(verified)
            context.set_progress(90)
            context.save_checkpoint(
                {
                    "stage": STAGE_REPORT,
                    "started_at": started_at,
                    "plan": plan.model_dump(mode="json"),
                    "plan_artifact_id": plan_artifact_id,
                    "candidates": candidates,
                    "collected": collected,
                    "evidence": evidence,
                    "claims": claims,
                    "verified": verified,
                    "contradictions": contradictions,
                }
            )
            stage = STAGE_REPORT
            checkpoint = {
                "stage": stage,
                "started_at": started_at,
                "plan": plan.model_dump(mode="json"),
                "plan_artifact_id": plan_artifact_id,
                "candidates": candidates,
                "collected": collected,
                "evidence": evidence,
                "claims": claims,
                "verified": verified,
                "contradictions": contradictions,
            }

        # --- REPORT ---
        completed_at = datetime.now(UTC)
        report = self._build_report(
            job=job,
            project_id=project_id,
            opportunity=opportunity,
            plan=plan,
            plan_artifact_id=plan_artifact_id,
            candidates=candidates,
            collected=collected,
            evidence=evidence,
            verified=verified,
            contradictions=contradictions,
            started_at=started_at,
            completed_at=completed_at,
            depth=config.depth,
            language=config.language,
        )
        record = self._artifact_store.save(
            ArtifactType.RESEARCH_REPORT,
            project_id,
            report,
            job_id=job.id,
            metadata={
                "job_type": job.type.value,
                "opportunity_id": opportunity.opportunity_id,
                "research_plan_id": plan_artifact_id,
            },
        )
        context.set_progress(100)
        return record.id

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_opportunity(self, job: JobRecord, config: ResearchJobConfig) -> OpportunityItem:
        """Load the opportunity from the opportunity_list artifact."""
        artifact_id = config.opportunity_list_artifact_id or job.input_artifact_id
        if artifact_id:
            _record, payload = self._artifact_store.load(artifact_id)
        else:
            if not job.project_id:
                raise ValueError("research job has no opportunity_list reference and no project")
            _record, payload = self._artifact_store.load_latest(
                job.project_id, ArtifactType.OPPORTUNITY_LIST
            )
        validated = validate_artifact_payload(ArtifactType.OPPORTUNITY_LIST, payload)
        opportunity_list = OpportunityList.model_validate(validated)
        if not opportunity_list.opportunities:
            raise ValueError("opportunity_list artifact contains no opportunities")
        if config.opportunity_id:
            for opportunity in opportunity_list.opportunities:
                if opportunity.opportunity_id == config.opportunity_id:
                    return opportunity
            raise ValueError(
                f"opportunity {config.opportunity_id!r} not found in the opportunity_list artifact"
            )
        return opportunity_list.opportunities[0]  # top-ranked by score

    def _discovery_queries(self, plan: ResearchPlan, opportunity: OpportunityItem) -> list[str]:
        """Deterministic discovery queries derived from the plan."""
        queries = [plan.central_question, opportunity.topic]
        for subquestion in plan.subquestions[:2]:
            queries.append(subquestion.question)
        # Deduplicated, order-preserving.
        seen: set[str] = set()
        unique: list[str] = []
        for query in queries:
            if query and query not in seen:
                seen.add(query)
                unique.append(query)
        return unique

    def _collected_to_item(self, source: CollectedSource) -> dict[str, Any]:
        from factory.research.sources import source_to_item_payload

        return source_to_item_payload(source)

    def _load_collected_sources(self, collected: list[dict[str, Any]]) -> list[CollectedSource]:
        """Rebuild CollectedSource objects. Content is loaded from the source
        cache by URL fingerprint — checkpoints never carry large content."""
        from factory.providers.research.types import SourceCandidate

        sources: list[CollectedSource] = []
        for item in collected:
            candidate = SourceCandidate.model_validate(item)
            content = ""
            if self._source_cache is not None:
                cached = self._source_cache.get(candidate.url_fingerprint)
                if cached is not None:
                    content = cached.content
            sources.append(
                CollectedSource(
                    candidate=candidate,
                    content=content,
                    content_type=item.get("content_type"),
                    byte_size=item.get("byte_size") or 0,
                    content_fingerprint=item.get("content_fingerprint"),
                    collection_status=item.get("collection_status", "collected"),
                    collection_error=item.get("collection_error"),
                    collected_at=item.get("collected_at"),
                )
            )
        return sources

    def _build_claims(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group evidence into claims by (subject, predicate)."""
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        unstructured: list[dict[str, Any]] = []
        for item in evidence:
            subject = (item.get("subject") or "").strip().lower()
            predicate = (item.get("predicate") or "").strip().lower()
            if subject and predicate:
                groups.setdefault((subject, predicate), []).append(item)
            else:
                unstructured.append(item)

        claims: list[dict[str, Any]] = []
        for (subject, predicate), items in sorted(groups.items()):
            # The claim value is the most frequent extracted value.
            value_counts: dict[str, int] = {}
            for item in items:
                if item.get("value"):
                    value_counts[item["value"]] = value_counts.get(item["value"], 0) + 1
            value = None
            value_type = None
            if value_counts:
                value = sorted(value_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                value_type = next(
                    (i.get("value_type") for i in items if i.get("value") == value), None
                )
            statement = (
                f"The {subject} {predicate} {value}." if value else f"The {subject} {predicate}."
            )
            claims.append(
                {
                    "statement": statement,
                    "claim_type": "fact",
                    "importance": "high" if value else "medium",
                    "evidence": items,
                    "subject": subject,
                    "predicate": predicate,
                    "value": value,
                    "value_type": value_type,
                }
            )
        # Unstructured evidence becomes low-importance text claims.
        for item in unstructured:
            claims.append(
                {
                    "statement": item["claim"],
                    "claim_type": "fact",
                    "importance": "low",
                    "evidence": [item],
                    "subject": None,
                    "predicate": None,
                    "value": None,
                    "value_type": None,
                }
            )
        return claims

    def _verified_payload(self, draft: ClaimDraft, result: Any) -> dict[str, Any]:
        """Map a verification result to the VerifiedClaim contract payload.

        The raw contradiction findings travel under ``_contradictions`` (an
        internal key, stripped before contract validation) so the report stage
        can assemble the contradiction summary.
        """
        return {
            "claim_id": result.claim_id,
            "statement": draft.statement,
            "claim_type": draft.claim_type,
            "importance": draft.importance,
            "evidence_refs": [e.source_id for e in draft.evidence],
            "source_count": len(result.supporting_source_ids)
            + len(result.contradicting_source_ids),
            "supporting_source_ids": result.supporting_source_ids,
            "contradicting_source_ids": result.contradicting_source_ids,
            "confidence": result.confidence,
            "verification_status": result.verification_status,
            "independent_source_count": result.independent_source_count,
            "notes": result.notes,
            "subject": draft.subject,
            "predicate": draft.predicate,
            "value": draft.value,
            "value_type": draft.value_type,
            "_contradictions": [
                finding.model_dump(mode="json") for finding in result.contradictions
            ],
        }

    def _collect_contradictions(self, verified: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Assemble contradiction details from verification results."""
        contradictions: list[dict[str, Any]] = []
        for index, claim in enumerate(verified):
            for finding in claim.get("_contradictions", []):
                contradictions.append(
                    {
                        "contradiction_id": f"con-{index:04d}",
                        "contradiction_type": finding["contradiction_type"],
                        "description": finding["description"],
                        "claim_refs": finding.get("claim_refs", []),
                        "values": finding.get("values", []),
                        "source_ids": finding.get("source_ids", []),
                        "resolution_status": "unresolved",
                        "stronger_authority_source_id": None,
                        "explanation": (
                            "Sources disagree; both sides are preserved with provenance. "
                            "The system does not silently choose a side."
                        ),
                    }
                )
        return contradictions

    def _build_report(
        self,
        *,
        job: JobRecord,
        project_id: str,
        opportunity: OpportunityItem,
        plan: ResearchPlan,
        plan_artifact_id: str | None,
        candidates: list[dict[str, Any]],
        collected: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        verified: list[dict[str, Any]],
        contradictions: list[dict[str, Any]],
        started_at: str,
        completed_at: datetime,
        depth: str,
        language: str | None,
    ) -> dict[str, Any]:
        """Assemble the research_report payload (new + legacy fields)."""

        def _contract_payload(v: dict[str, Any]) -> dict[str, Any]:
            clean = {k: value for k, value in v.items() if k != "_contradictions"}
            return VerifiedClaim.model_validate(clean).model_dump(mode="json")

        verified_claims = [
            _contract_payload(v)
            for v in verified
            if v["verification_status"] in ("SUPPORTED", "MULTI_SOURCE_SUPPORTED")
        ]
        contested_claims = [
            _contract_payload(v)
            for v in verified
            if v["verification_status"] in ("CONTESTED", "CONTRADICTED")
        ]
        insufficient = [v for v in verified if v["verification_status"] == "INSUFFICIENT_EVIDENCE"]

        source_items = [SourceItem.model_validate(c) for c in collected]
        evidence_items = [EvidenceItem.model_validate(e) for e in evidence]

        # Executive findings (generated from verified claims + gaps).
        findings: list[str] = []
        if verified_claims:
            strongest = max(verified_claims, key=lambda c: c["confidence"])
            findings.append(
                f"Strongest verified finding ({strongest['verification_status']}): "
                f"{strongest['statement']}"
            )
        findings.append(
            f"{len(verified_claims)} claim(s) verified, {len(contested_claims)} contested, "
            f"{len(insufficient)} without sufficient evidence, across "
            f"{len(source_items)} collected source(s)."
        )
        if contradictions:
            findings.append(
                f"{len(contradictions)} contradiction(s) detected and preserved with "
                "both sides — not silently resolved."
            )
        unresolved = [
            sub.question
            for sub in plan.subquestions
            if any(sub.question.lower() in (v.get("notes") or "").lower() for v in insufficient)
        ] or [sub.question for sub in plan.subquestions if sub.category == "disputed"]

        # Source quality aggregates.
        type_counts: dict[str, int] = {}
        for source in source_items:
            type_counts[source.source_type] = type_counts.get(source.source_type, 0) + 1
        collected_ok = [s for s in source_items if s.collection_status in ("collected", "cached")]
        source_quality = {
            "total_candidates": len(candidates),
            "collected": len(collected_ok),
            "failed": len(source_items) - len(collected_ok),
            "by_type": type_counts,
            "average_authority": round(
                sum(s.authority_score for s in source_items) / len(source_items), 4
            )
            if source_items
            else 0.0,
        }

        confidence_summary = {
            "verified_claims": len(verified_claims),
            "contested_claims": len(contested_claims),
            "insufficient_evidence_claims": len(insufficient),
            "average_confidence": round(sum(v["confidence"] for v in verified) / len(verified), 4)
            if verified
            else 0.0,
            "multi_source_supported": sum(
                1 for v in verified if v["verification_status"] == "MULTI_SOURCE_SUPPORTED"
            ),
        }

        # Legacy Phase 0 fields (backward compatibility).
        legacy_claims = [
            {
                "claim": v["statement"],
                "support_status": (
                    "supported"
                    if v["verification_status"] in ("SUPPORTED", "MULTI_SOURCE_SUPPORTED")
                    else "contradicted"
                    if v["verification_status"] in ("CONTESTED", "CONTRADICTED")
                    else "unverified"
                ),
                "confidence": v["confidence"],
                "source_urls": [
                    next(
                        (s.url for s in source_items if s.source_id in v["supporting_source_ids"]),
                        "",
                    )
                    for _ in v["supporting_source_ids"]
                ],
            }
            for v in verified
        ]
        legacy_sources = [
            {
                "source_id": s.source_id,
                "url": s.url,
                "title": s.title,
                "retrieved_at": s.collected_at or completed_at,
            }
            for s in source_items
        ]
        legacy_contradictions = [
            {"description": c["description"], "claim_refs": c["claim_refs"]} for c in contradictions
        ]

        summary = (
            f"Research on '{opportunity.topic}' for opportunity "
            f"{opportunity.opportunity_id}: {len(verified_claims)} verified, "
            f"{len(contested_claims)} contested, {len(insufficient)} insufficient-evidence "
            f"claims from {len(collected_ok)} collected source(s)."
        )

        return ResearchReport(
            research_report_id=job.id,
            opportunity_id=opportunity.opportunity_id,
            topic=opportunity.topic,
            generated_at=completed_at,
            summary=summary,
            sources=legacy_sources,
            claims=legacy_claims,
            contradictions=legacy_contradictions,
            project_id=project_id,
            research_question=plan.central_question,
            executive_findings=findings,
            verified_claims=verified_claims,
            contested_claims=contested_claims,
            unresolved_questions=unresolved,
            evidence=[e.model_dump(mode="json") for e in evidence_items],
            source_list=[s.model_dump(mode="json") for s in source_items],
            source_quality=source_quality,
            contradiction_details=contradictions,
            confidence_summary=confidence_summary,
            limitations=[
                "Deterministic extraction and verification; an LLM-backed extractor "
                "can be added behind the EvidenceExtractor contract.",
                "Sources are limited to what the configured search API returns; "
                "no source is treated as authoritative without a justified signal.",
                "Contradictions are preserved unresolved; the system never invents a resolution.",
            ],
            research_plan_id=plan_artifact_id,
            depth=depth,
            language=language,
            started_at=datetime.fromisoformat(started_at),
            completed_at=completed_at,
        ).model_dump(mode="json")

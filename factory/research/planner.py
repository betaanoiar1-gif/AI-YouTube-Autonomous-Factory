"""Deterministic research planner (ResearchProvider implementation).

Derives a structured research plan from an opportunity. The plan is generated
from the opportunity's topic and audience question — it never copies
competitor titles or descriptions (originality boundary).

Deterministic: the same opportunity + inputs produce the same plan. An
LLM-backed planner can implement the same contract later without changing the
engine.
"""

from __future__ import annotations

from datetime import UTC, datetime

from factory.providers.research.base import ResearchProvider
from factory.schemas.artifacts import (
    OpportunityItem,
    ResearchPlan,
    ResearchSubquestion,
)


class DeterministicResearchPlanner(ResearchProvider):
    """Deterministic, provider-independent research planning."""

    name = "deterministic-planner"

    def plan_research(
        self,
        opportunity: OpportunityItem,
        *,
        project_id: str,
        depth: str = "standard",
        language: str | None = None,
        target_audience: str | None = None,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> ResearchPlan:
        topic = opportunity.topic.strip()
        central_question = (
            opportunity.audience_question
            or f"What should be independently researched about {topic}?"
        )

        subquestions = [
            ResearchSubquestion(
                question=f"What is the historical context and origin of {topic}?",
                category="historical",
                priority=1,
            ),
            ResearchSubquestion(
                question=f"What are the key facts, numbers, and dates about {topic}?",
                category="quantitative",
                priority=1,
            ),
            ResearchSubquestion(
                question=f"What is disputed, uncertain, or contradictory about {topic}?",
                category="disputed",
                priority=2,
            ),
            ResearchSubquestion(
                question=f"Who are the main people, organizations, or places involved in {topic}?",
                category="contextual",
                priority=2,
            ),
        ]
        if depth == "deep":
            subquestions.extend(
                [
                    ResearchSubquestion(
                        question=f"How does {topic} compare with related topics or approaches?",
                        category="comparative",
                        priority=3,
                    ),
                    ResearchSubquestion(
                        question=f"What primary sources exist for {topic}?",
                        category="contextual",
                        priority=3,
                    ),
                ]
            )

        required_facts = [
            f"the key dates and timeline of {topic}",
            f"the key numbers, measurements, and quantities about {topic}",
            f"the main people, organizations, and places involved in {topic}",
        ]
        if depth == "deep":
            required_facts.append(f"primary-source material about {topic}")

        source_requirements = [
            "reference sources for background and definitions",
            "primary or institutional (government/academic) sources for dates and numbers",
            "reputable journalism for recent context where applicable",
        ]
        verification_requirements = [
            "important claims require at least two independent sources",
            "prefer primary/institutional sources for numbers and dates",
            "contradictions must be preserved with both sides, never silently resolved",
            "absence of evidence is never treated as confirmation",
        ]

        return ResearchPlan(
            research_plan_id=f"plan-{job_id or 'adhoc'}",
            project_id=project_id,
            opportunity_id=opportunity.opportunity_id,
            central_question=central_question,
            subquestions=subquestions,
            required_facts=required_facts,
            source_requirements=source_requirements,
            verification_requirements=verification_requirements,
            depth=depth if depth in ("standard", "deep") else "standard",
            language=language,
            target_audience=target_audience,
            generated_at=now or datetime.now(UTC),
        )

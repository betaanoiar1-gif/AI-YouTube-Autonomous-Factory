"""Research planner unit tests: opportunity → plan, determinism, no copying."""

from __future__ import annotations

from datetime import UTC, datetime

from factory.research.planner import DeterministicResearchPlanner
from factory.schemas.artifacts import OpportunityItem


def make_opportunity(**overrides) -> OpportunityItem:
    base = {
        "opportunity_id": "opp-1",
        "title": "Original coverage: ancient aqueducts",
        "topic": "ancient aqueducts",
        "score": 72.5,
        "rationale": "Underserved recurring theme.",
        "audience_question": "What should viewers know about ancient aqueducts?",
        "supporting_video_ids": ["vid-comp-1"],
        "confidence": 0.8,
    }
    base.update(overrides)
    return OpportunityItem(**base)


class TestResearchPlan:
    def test_plan_structure(self) -> None:
        plan = DeterministicResearchPlanner().plan_research(
            make_opportunity(), project_id="proj-1", job_id="job-1"
        )
        assert plan.project_id == "proj-1"
        assert plan.opportunity_id == "opp-1"
        assert plan.central_question == "What should viewers know about ancient aqueducts?"
        assert len(plan.subquestions) >= 4
        categories = {s.category for s in plan.subquestions}
        assert {"historical", "quantitative", "disputed"} <= categories
        assert plan.required_facts
        assert plan.source_requirements
        assert plan.verification_requirements
        assert plan.depth == "standard"

    def test_deep_depth_adds_subquestions(self) -> None:
        planner = DeterministicResearchPlanner()
        standard = planner.plan_research(make_opportunity(), project_id="p", depth="standard")
        deep = planner.plan_research(make_opportunity(), project_id="p", depth="deep")
        assert len(deep.subquestions) > len(standard.subquestions)
        assert len(deep.required_facts) > len(standard.required_facts)
        assert deep.depth == "deep"

    def test_default_central_question_when_missing(self) -> None:
        plan = DeterministicResearchPlanner().plan_research(
            make_opportunity(audience_question=None), project_id="p"
        )
        assert "ancient aqueducts" in plan.central_question

    def test_deterministic(self) -> None:
        planner = DeterministicResearchPlanner()
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        first = planner.plan_research(make_opportunity(), project_id="p", job_id="j", now=moment)
        second = planner.plan_research(make_opportunity(), project_id="p", job_id="j", now=moment)
        assert first.model_dump(mode="json") == second.model_dump(mode="json")

    def test_no_competitor_content_copied(self) -> None:
        """The plan is generated from the topic — never from competitor titles."""
        opportunity = make_opportunity(
            supporting_video_ids=["vid-comp-1", "vid-comp-2"],
        )
        plan = DeterministicResearchPlanner().plan_research(opportunity, project_id="p")
        blob = plan.model_dump_json()
        competitor_titles = {"The forgotten tunnels of Paris", "Forgotten tunnels under the city"}
        for title in competitor_titles:
            assert title not in blob
        # The plan references the opportunity's topic, not video content:
        assert "ancient aqueducts" in plan.central_question

    def test_language_and_audience_propagated(self) -> None:
        plan = DeterministicResearchPlanner().plan_research(
            make_opportunity(), project_id="p", language="en", target_audience="US viewers"
        )
        assert plan.language == "en"
        assert plan.target_audience == "US viewers"

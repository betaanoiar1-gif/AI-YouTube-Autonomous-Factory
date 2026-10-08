"""Phase 1 artifact-contract tests: extended contracts + backward compatibility.

* The extended contracts (richer discovery/analysis/opportunity payloads)
  validate against BOTH pydantic and JSON Schema 2020-12.
* The Phase 0 minimal payloads still validate (backward-compatible extension:
  all new fields are optional).
* Invalid payloads are rejected by both representations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JSONSchemaValidationError

from factory.errors import ArtifactValidationError
from factory.schemas.artifacts import (
    ARTIFACT_CONTRACTS,
    ArtifactType,
    load_json_schema,
    validate_artifact_payload,
)

SCHEMAS_DIR = Path(__file__).resolve().parent.parent.parent / "schemas" / "artifacts"

RICH_DISCOVERY: dict[str, Any] = {
    "discovery_id": "d1",
    "project_id": "p1",
    "query": "history mystery",
    "language": "en",
    "target_audience": "history enthusiasts",
    "search_parameters": {"q": "history mystery", "order": "relevance"},
    "quota_units_used": 202,
    "provider": {"name": "youtube", "quota_units_used": 202},
    "videos": [
        {
            "video_id": "vid1",
            "channel_id": "UC1",
            "title": "The forgotten tunnels",
            "url": "https://www.youtube.com/watch?v=vid1",
            "duration_seconds": 900,
            "published_at": "2024-05-01T12:00:00Z",
            "views": 1000,
            "likes": 50,
            "comments": 10,
            "channel_title": "History Underground",
            "description_chars": 195,
            "tags": ["history", "tunnels"],
            "category_id": "22",
            "definition": "hd",
        }
    ],
    "channels": [
        {
            "channel_id": "UC1",
            "title": "History Underground",
            "subscriber_count": 500000,
            "view_count": 10000000,
            "video_count": 200,
            "retrieved_at": "2025-01-01T00:00:00Z",
        }
    ],
}

RICH_ANALYSIS: dict[str, Any] = {
    "analysis_id": "a1",
    "project_id": "p1",
    "video_count": 1,
    "total_views": 1000,
    "average_views": 1000.0,
    "median_views": 1000.0,
    "channel_count": 1,
    "top_performer_video_ids": ["vid1"],
    "source_artifact_id": "art-1",
    "competition": {"video_count": 1, "channel_count": 1, "top_channel_view_share": 1.0},
    "scoring": {"weights": {"performance": 0.3}, "formulas": {"composite": "..."}},
    "videos": [
        {
            "video_id": "vid1",
            "channel_id": "UC1",
            "title": "The forgotten tunnels",
            "views": 1000,
            "likes": 50,
            "comments": 10,
            "duration_seconds": 900,
            "published_at": "2024-05-01T12:00:00Z",
            "age_days": 10.0,
            "views_per_day": 100.0,
            "engagement_rate": 0.06,
            "comments_per_1000_views": 10.0,
            "channel_average_views": 1000.0,
            "channel_relative_performance": 1.0,
            "sub_scores": {"performance": 1.0, "velocity": 1.0},
            "composite_score": 80.0,
        }
    ],
    "patterns": [
        {
            "pattern": "forgotten tunnels",
            "pattern_type": "topic",
            "supporting_video_ids": ["vid1"],
            "confidence": 1.0,
        }
    ],
}

RICH_OPPORTUNITIES: dict[str, Any] = {
    "opportunity_list_id": "o1",
    "project_id": "p1",
    "source_artifact_id": "art-2",
    "opportunities": [
        {
            "opportunity_id": "opp-1",
            "title": "Original coverage: forgotten tunnels",
            "topic": "forgotten tunnels",
            "score": 72.5,
            "rationale": "Demand is measurable while competition is low.",
            "content_gap": "No original primary-source coverage exists.",
            "audience_question": "What should viewers know about forgotten tunnels?",
            "evidence_refs": ["analysis:a1:topic:forgotten tunnels", "video:vid1"],
            "supporting_video_ids": ["vid1"],
            "demand_signals": {"supporting_video_count": 1, "total_views": 1000},
            "competition_signals": {"saturation": "underserved", "channel_count": 1},
            "novelty_rationale": "Classified underserved by the clusterer.",
            "confidence": 0.8,
            "recommended_angle": "Produce an original, evidence-based video.",
        }
    ],
    "clusters": [
        {
            "cluster_id": "c1",
            "label": "forgotten tunnels",
            "video_count": 1,
            "total_views": 1000,
            "average_views": 1000.0,
            "saturation": "underserved",
            "representative_video_ids": ["vid1"],
        }
    ],
}

# The Phase 0 minimal payloads (unchanged) — backward compatibility.
PHASE0_DISCOVERY = {
    "discovery_id": "d1",
    "project_id": "p1",
    "query": "q",
    "videos": [
        {
            "video_id": "vid1",
            "channel_id": "ch1",
            "title": "T",
            "url": "https://youtube.com/watch?v=vid1",
        }
    ],
}

PHASE0_ANALYSIS = {
    "analysis_id": "a1",
    "project_id": "p1",
    "video_count": 25,
}

PHASE0_OPPORTUNITIES = {
    "opportunity_list_id": "o1",
    "project_id": "p1",
    "opportunities": [
        {
            "title": "The Bandit Queen of the Silk Road",
            "topic": "historical mysteries",
            "score": 87.5,
            "rationale": "Underexplored topic with proven formats",
        }
    ],
}


@pytest.mark.parametrize(
    ("artifact_type", "payload"),
    [
        (ArtifactType.DISCOVERY_RESULT, RICH_DISCOVERY),
        (ArtifactType.ANALYSIS_RESULT, RICH_ANALYSIS),
        (ArtifactType.OPPORTUNITY_LIST, RICH_OPPORTUNITIES),
    ],
    ids=["discovery_result", "analysis_result", "opportunity_list"],
)
class TestRichPayloadsValidate:
    def test_pydantic(self, artifact_type, payload):
        normalized = validate_artifact_payload(artifact_type, payload)
        assert normalized["schema_version"] == "1.0.0"

    def test_json_schema(self, artifact_type, payload):
        schema = load_json_schema(artifact_type, SCHEMAS_DIR)
        Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize(
    ("artifact_type", "payload"),
    [
        (ArtifactType.DISCOVERY_RESULT, PHASE0_DISCOVERY),
        (ArtifactType.ANALYSIS_RESULT, PHASE0_ANALYSIS),
        (ArtifactType.OPPORTUNITY_LIST, PHASE0_OPPORTUNITIES),
    ],
    ids=["phase0_discovery", "phase0_analysis", "phase0_opportunities"],
)
class TestPhase0PayloadsStillValidate:
    """Backward compatibility: Phase 0 payloads remain valid contracts."""

    def test_pydantic(self, artifact_type, payload):
        normalized = validate_artifact_payload(artifact_type, payload)
        assert normalized["schema_version"] == "1.0.0"

    def test_json_schema(self, artifact_type, payload):
        schema = load_json_schema(artifact_type, SCHEMAS_DIR)
        Draft202012Validator(schema).validate(payload)


class TestInvalidPayloadsRejected:
    def test_discovery_video_without_id(self):
        payload = {**RICH_DISCOVERY, "videos": [{"channel_id": "UC1", "title": "t", "url": "u"}]}
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.DISCOVERY_RESULT, payload)
        schema = load_json_schema(ArtifactType.DISCOVERY_RESULT, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_analysis_composite_score_out_of_bounds(self):
        payload = {
            **RICH_ANALYSIS,
            "videos": [{**RICH_ANALYSIS["videos"][0], "composite_score": 150.0}],
        }
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.ANALYSIS_RESULT, payload)
        schema = load_json_schema(ArtifactType.ANALYSIS_RESULT, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_opportunity_score_out_of_bounds(self):
        payload = {
            **RICH_OPPORTUNITIES,
            "opportunities": [{**RICH_OPPORTUNITIES["opportunities"][0], "score": 101.0}],
        }
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.OPPORTUNITY_LIST, payload)
        schema = load_json_schema(ArtifactType.OPPORTUNITY_LIST, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_unknown_field_rejected(self):
        payload = {**RICH_DISCOVERY, "sneaky_field": True}
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.DISCOVERY_RESULT, payload)
        schema = load_json_schema(ArtifactType.DISCOVERY_RESULT, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)


def test_all_contracts_have_matching_files():
    for artifact_type, contract in ARTIFACT_CONTRACTS.items():
        assert (SCHEMAS_DIR / contract.schema_file).exists(), artifact_type

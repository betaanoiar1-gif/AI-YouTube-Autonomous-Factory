"""Artifact contract tests: every type has a pydantic model + JSON Schema;
sample payloads validate against both; invalid payloads fail both."""

from __future__ import annotations

from pathlib import Path

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

VALID_SAMPLES: dict[ArtifactType, dict] = {
    ArtifactType.DISCOVERY_RESULT: {
        "discovery_id": "d1",
        "project_id": "p1",
        "query": "historical mysteries",
        "videos": [
            {
                "video_id": "vid1",
                "channel_id": "ch1",
                "title": "The Tunnel",
                "url": "https://youtube.com/watch?v=vid1",
                "duration_seconds": 1200,
                "views": 100000,
                "likes": 5000,
                "comments": 300,
            }
        ],
        "quota_units_used": 10,
    },
    ArtifactType.ANALYSIS_RESULT: {
        "analysis_id": "a1",
        "project_id": "p1",
        "video_count": 25,
        "total_views": 2500000,
        "average_views": 100000.0,
        "top_performer_video_ids": ["vid1"],
        "patterns": [
            {"pattern": "mystery framing", "supporting_video_ids": ["vid1"], "confidence": 0.8}
        ],
    },
    ArtifactType.OPPORTUNITY_LIST: {
        "opportunity_list_id": "o1",
        "project_id": "p1",
        "opportunities": [
            {
                "title": "The Bandit Queen of the Silk Road",
                "topic": "historical mysteries",
                "score": 87.5,
                "rationale": "Underexplored topic with proven formats",
                "content_gap": "No documentary covers primary sources",
            }
        ],
    },
    ArtifactType.RESEARCH_REPORT: {
        "research_report_id": "r1",
        "topic": "silk road bandits",
        "summary": "Research summary of the topic.",
        "sources": [{"url": "https://example.com/doc", "title": "Doc"}],
        "claims": [
            {
                "claim": "The route was active in the 1200s",
                "support_status": "supported",
                "confidence": 0.9,
                "source_urls": ["https://example.com/doc"],
            }
        ],
        "contradictions": [{"description": "Two dates conflict", "claim_refs": ["c1", "c2"]}],
    },
    ArtifactType.CONTENT_BRIEF: {
        "brief_id": "b1",
        "opportunity_id": "opp1",
        "title": "Title",
        "angle": "Angle",
        "target_audience": "US viewers",
        "key_points": ["k1"],
        "content_gaps_addressed": ["g1"],
        "estimated_duration_seconds": 600,
    },
    ArtifactType.NARRATIVE_OUTLINE: {
        "outline_id": "outline-1",
        "brief_id": "brief-1",
        "opportunity_id": "opp-1",
        "title": "Ancient Aqueducts",
        "beats": [
            {
                "beat_id": "hook-1",
                "index": 0,
                "beat_type": "hook",
                "title": "The central puzzle",
                "purpose": "What made this system work?",
            }
        ],
        "estimated_duration_seconds": 600,
        "source_artifact_ids": ["opp-artifact", "research-artifact", "brief-artifact"],
    },
    ArtifactType.SCRIPT: {
        "script_id": "s1",
        "title": "Script title",
        "language": "en",
        "scenes": [
            {
                "scene_id": "sc1",
                "index": 0,
                "narration": "Narration text.",
                "duration_seconds": 30.0,
                "shots": [
                    {
                        "shot_id": "sh1",
                        "index": 0,
                        "shot_type": "b-roll",
                        "description": "Establishing shot",
                        "duration_seconds": 5.0,
                        "asset_requirements": ["archive photo"],
                    }
                ],
            }
        ],
        "word_count": 500,
        "total_duration_seconds": 600.0,
    },
    ArtifactType.STORYBOARD: {
        "storyboard_id": "sb1",
        "script_id": "s1",
        "scenes": [
            {
                "scene_id": "sc1",
                "narration_excerpt": "Narration text.",
                "shots": [
                    {
                        "shot_id": "sh1",
                        "index": 0,
                        "shot_type": "b-roll",
                        "description": "Establishing shot",
                        "duration_seconds": 5.0,
                        "asset_hints": ["stock footage"],
                    }
                ],
            }
        ],
        "total_duration_seconds": 600.0,
    },
    ArtifactType.PRODUCTION_TIMELINE: {
        "timeline_id": "t1",
        "tracks": [
            {
                "track_type": "voice",
                "clips": [
                    {
                        "clip_id": "c1",
                        "start_seconds": 0.0,
                        "duration_seconds": 30.0,
                        "scene_id": "sc1",
                    }
                ],
            },
            {
                "track_type": "subtitle",
                "clips": [
                    {
                        "clip_id": "c2",
                        "start_seconds": 0.0,
                        "duration_seconds": 30.0,
                        "text": "Hello",
                    }
                ],
            },
        ],
        "total_duration_seconds": 600.0,
    },
    ArtifactType.QA_REPORT: {
        "report_id": "qa1",
        "target_artifact_id": "s1",
        "target_artifact_type": "script",
        "checks": [
            {"check_id": "factual-1", "category": "factual", "status": "pass", "details": "ok"}
        ],
        "overall_status": "pass",
        "summary": "All checks passed",
    },
    ArtifactType.PUBLISH_PACKAGE: {
        "package_id": "pp1",
        "title": "The Forgotten Tunnel",
        "title_options": ["Alt title"],
        "description": "A description.",
        "tags": ["history", "mystery"],
        "category": "Education",
        "language": "en",
        "thumbnail_concept": {
            "description": "A dark tunnel with a lantern",
            "text_overlay": "THE FORGOTTEN TUNNEL",
            "style_notes": "cinematic",
        },
    },
    ArtifactType.RESEARCH_PLAN: {
        "research_plan_id": "rp1",
        "project_id": "p1",
        "opportunity_id": "opp-1",
        "central_question": "What should viewers know about ancient aqueducts?",
        "subquestions": [
            {
                "question": "When were the ancient aqueducts built?",
                "category": "historical",
                "priority": 1,
            },
            {
                "question": "How much water did they carry?",
                "category": "quantitative",
                "priority": 2,
            },
        ],
        "required_facts": ["construction dates", "water capacity"],
        "source_requirements": ["reference", "academic"],
        "verification_requirements": ["two independent sources for important claims"],
        "depth": "standard",
        "language": "en",
    },
    ArtifactType.SOURCE: {
        "source_id": "src-1",
        "url": "https://example.org/aqueducts",
        "canonical_url": "https://example.org/aqueducts",
        "url_fingerprint": "a" * 64,
        "title": "Ancient Aqueducts Reference",
        "publisher": "Example Reference",
        "source_type": "reference",
        "discovery_query": "ancient aqueducts history",
        "relevance_score": 0.9,
        "authority_score": 0.7,
        "authority_indicators": ["reference-domain"],
        "collection_status": "collected",
        "content_fingerprint": "b" * 64,
        "byte_size": 12345,
        "content_type": "text/html",
    },
    ArtifactType.EVIDENCE: {
        "evidence_id": "ev-1",
        "source_id": "src-1",
        "claim": "The Aqua Aqueduct was completed in 312.",
        "passage": "The Aqua Aqueduct was completed in 312.",
        "location": "paragraph 2",
        "confidence": 0.9,
        "subject": "aqua aqueduct",
        "predicate": "completed in",
        "value": "312",
        "value_type": "date",
    },
    ArtifactType.RESEARCH_CLAIM: {
        "claim_id": "cl-1",
        "statement": "The Aqua Aqueduct was completed in 312.",
        "claim_type": "fact",
        "importance": "high",
        "evidence_refs": ["ev-1"],
        "source_count": 1,
        "supporting_source_ids": ["src-1"],
        "contradicting_source_ids": [],
        "confidence": 0.8,
        "verification_status": "SUPPORTED",
        "independent_source_count": 1,
        "subject": "aqua aqueduct",
        "predicate": "completed in",
        "value": "312",
        "value_type": "date",
    },
}

INVALID_SAMPLES: dict[ArtifactType, dict] = {
    artifact_type: {**sample, "schema_version": "not-semver"}
    for artifact_type, sample in VALID_SAMPLES.items()
}


def test_every_type_has_contract_and_schema_file():
    assert set(ArtifactType) == set(ARTIFACT_CONTRACTS.keys())
    for artifact_type, contract in ARTIFACT_CONTRACTS.items():
        schema_path = SCHEMAS_DIR / contract.schema_file
        assert schema_path.exists(), f"missing JSON schema for {artifact_type}"
        assert contract.schema_version == "1.0.0"


@pytest.mark.parametrize("artifact_type", list(ArtifactType), ids=lambda t: t.value)
def test_valid_sample_passes_pydantic_and_json_schema(artifact_type):
    payload = VALID_SAMPLES[artifact_type]
    normalized = validate_artifact_payload(artifact_type, payload)
    assert normalized["schema_version"] == "1.0.0"

    schema = load_json_schema(artifact_type, SCHEMAS_DIR)
    Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize("artifact_type", list(ArtifactType), ids=lambda t: t.value)
def test_invalid_sample_fails_pydantic_and_json_schema(artifact_type):
    payload = INVALID_SAMPLES[artifact_type]
    with pytest.raises(ArtifactValidationError):
        validate_artifact_payload(artifact_type, payload)

    schema = load_json_schema(artifact_type, SCHEMAS_DIR)
    with pytest.raises(JSONSchemaValidationError):
        Draft202012Validator(schema).validate(payload)


def test_unknown_artifact_type_rejected():
    with pytest.raises(ArtifactValidationError):
        validate_artifact_payload("not_a_type", {})


def test_json_schema_is_draft_2020_12():
    for artifact_type in ArtifactType:
        schema = load_json_schema(artifact_type, SCHEMAS_DIR)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False

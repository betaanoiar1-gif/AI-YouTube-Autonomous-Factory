"""Phase 2 artifact-contract tests: new research contracts + backward compat.

* research_plan / source / evidence / research_claim validate against BOTH
  pydantic and JSON Schema 2020-12.
* The extended research_report validates rich payloads AND the Phase 0
  minimal payload still validates (backward compatibility).
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

RESEARCH_PLAN: dict[str, Any] = {
    "research_plan_id": "rp1",
    "project_id": "p1",
    "opportunity_id": "opp-1",
    "central_question": "What should viewers know about ancient aqueducts?",
    "subquestions": [
        {"question": "When were they built?", "category": "historical", "priority": 1}
    ],
    "required_facts": ["dates"],
    "source_requirements": ["reference"],
    "verification_requirements": ["two independent sources"],
    "depth": "standard",
}

SOURCE: dict[str, Any] = {
    "source_id": "src-1",
    "url": "https://example.org/a",
    "canonical_url": "https://example.org/a",
    "url_fingerprint": "a" * 64,
    "title": "A source",
    "source_type": "reference",
    "collection_status": "collected",
    "authority_score": 0.6,
    "authority_indicators": ["reference-domain:example.org"],
    "content_fingerprint": "b" * 64,
}

EVIDENCE: dict[str, Any] = {
    "evidence_id": "ev-1",
    "source_id": "src-1",
    "claim": "The Aqua Aqueduct was completed in 1312.",
    "passage": "The Aqua Aqueduct was completed in 1312.",
    "location": "sentence 1",
    "confidence": 0.9,
    "subject": "aqua aqueduct",
    "predicate": "was completed in",
    "value": "1312",
    "value_type": "date",
}

RESEARCH_CLAIM: dict[str, Any] = {
    "claim_id": "cl-1",
    "statement": "The Aqua Aqueduct was completed in 1312.",
    "verification_status": "MULTI_SOURCE_SUPPORTED",
    "evidence_refs": ["ev-1"],
    "supporting_source_ids": ["src-1", "src-2"],
    "independent_source_count": 2,
    "confidence": 0.9,
}

RICH_REPORT: dict[str, Any] = {
    "research_report_id": "rr1",
    "opportunity_id": "opp-1",
    "topic": "ancient aqueducts",
    "summary": "Research summary.",
    "project_id": "p1",
    "research_question": "What should viewers know about ancient aqueducts?",
    "executive_findings": ["Finding one."],
    "verified_claims": [RESEARCH_CLAIM],
    "contested_claims": [],
    "unresolved_questions": ["What is disputed?"],
    "evidence": [EVIDENCE],
    "source_list": [SOURCE],
    "source_quality": {"collected": 1},
    "contradiction_details": [
        {
            "contradiction_id": "con-1",
            "contradiction_type": "date",
            "description": "Sources disagree on the date.",
            "values": ["1312", "1305"],
            "source_ids": ["src-1", "src-2"],
            "resolution_status": "unresolved",
        }
    ],
    "confidence_summary": {"verified_claims": 1},
    "limitations": ["Deterministic extraction."],
    "research_plan_id": "rp1",
    "depth": "standard",
    "started_at": "2026-10-08T10:00:00Z",
    "completed_at": "2026-10-08T10:05:00Z",
}

# Phase 0 minimal research_report (unchanged) — backward compatibility.
PHASE0_REPORT: dict[str, Any] = {
    "research_report_id": "rr1",
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
}


@pytest.mark.parametrize(
    ("artifact_type", "payload"),
    [
        (ArtifactType.RESEARCH_PLAN, RESEARCH_PLAN),
        (ArtifactType.SOURCE, SOURCE),
        (ArtifactType.EVIDENCE, EVIDENCE),
        (ArtifactType.RESEARCH_CLAIM, RESEARCH_CLAIM),
        (ArtifactType.RESEARCH_REPORT, RICH_REPORT),
    ],
    ids=["research_plan", "source", "evidence", "research_claim", "rich_report"],
)
class TestPhase2ContractsValidate:
    def test_pydantic(self, artifact_type, payload):
        normalized = validate_artifact_payload(artifact_type, payload)
        assert normalized["schema_version"] == "1.0.0"

    def test_json_schema(self, artifact_type, payload):
        schema = load_json_schema(artifact_type, SCHEMAS_DIR)
        Draft202012Validator(schema).validate(payload)


class TestBackwardCompatibility:
    def test_phase0_report_still_valid_pydantic(self):
        normalized = validate_artifact_payload(ArtifactType.RESEARCH_REPORT, PHASE0_REPORT)
        assert normalized["schema_version"] == "1.0.0"

    def test_phase0_report_still_valid_json_schema(self):
        schema = load_json_schema(ArtifactType.RESEARCH_REPORT, SCHEMAS_DIR)
        Draft202012Validator(schema).validate(PHASE0_REPORT)


class TestInvalidPayloadsRejected:
    def test_source_missing_required_field(self):
        payload = {k: v for k, v in SOURCE.items() if k != "url_fingerprint"}
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.SOURCE, payload)
        schema = load_json_schema(ArtifactType.SOURCE, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_claim_confidence_out_of_bounds(self):
        payload = {**RESEARCH_CLAIM, "confidence": 1.5}
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.RESEARCH_CLAIM, payload)
        schema = load_json_schema(ArtifactType.RESEARCH_CLAIM, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_report_unknown_field_rejected(self):
        payload = {**RICH_REPORT, "sneaky": True}
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.RESEARCH_REPORT, payload)
        schema = load_json_schema(ArtifactType.RESEARCH_REPORT, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_plan_missing_central_question(self):
        payload = {k: v for k, v in RESEARCH_PLAN.items() if k != "central_question"}
        with pytest.raises(ArtifactValidationError):
            validate_artifact_payload(ArtifactType.RESEARCH_PLAN, payload)
        schema = load_json_schema(ArtifactType.RESEARCH_PLAN, SCHEMAS_DIR)
        with pytest.raises(JSONSchemaValidationError):
            Draft202012Validator(schema).validate(payload)


def test_all_new_contracts_have_matching_files():
    for artifact_type in (
        ArtifactType.RESEARCH_PLAN,
        ArtifactType.SOURCE,
        ArtifactType.EVIDENCE,
        ArtifactType.RESEARCH_CLAIM,
        ArtifactType.RESEARCH_REPORT,
    ):
        contract = ARTIFACT_CONTRACTS[artifact_type]
        assert (SCHEMAS_DIR / contract.schema_file).exists(), artifact_type

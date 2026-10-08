"""Artifact contracts: types, pydantic models, and the validation registry.

Each artifact type has:

* a stable ``ArtifactType`` value (stored in the ``artifacts`` table);
* a ``schema_version`` (semver; bumped on breaking contract changes — old
  artifacts remain readable because they are never overwritten);
* a pydantic model (runtime validation, ``extra="forbid"`` so contracts are
  strict and payloads cannot smuggle unexpected fields);
* a JSON Schema file (``schemas/artifacts/<type>.schema.json``).

Validation rejects malformed payloads — invalid output is never silently
accepted by the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from factory.errors import ArtifactValidationError

ARTIFACT_SCHEMA_VERSION = "1.0.0"


class ArtifactModel(BaseModel):
    """Base config for artifact payload models: strict, versioned."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=ARTIFACT_SCHEMA_VERSION, pattern=r"^\d+\.\d+\.\d+$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ArtifactType(StrEnum):
    """All pipeline artifact types (one per major pipeline stage)."""

    DISCOVERY_RESULT = "discovery_result"
    ANALYSIS_RESULT = "analysis_result"
    OPPORTUNITY_LIST = "opportunity_list"
    RESEARCH_REPORT = "research_report"
    CONTENT_BRIEF = "content_brief"
    SCRIPT = "script"
    STORYBOARD = "storyboard"
    PRODUCTION_TIMELINE = "production_timeline"
    QA_REPORT = "qa_report"
    PUBLISH_PACKAGE = "publish_package"


# ---------------------------------------------------------------------------
# Discovery (intelligence plane)
# ---------------------------------------------------------------------------


class DiscoveredVideo(ArtifactModel):
    """A video discovered during discovery, with a metrics snapshot."""

    video_id: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1, max_length=500)
    duration_seconds: int | None = Field(default=None, ge=0)
    published_at: datetime | None = None
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)


class DiscoveryResult(ArtifactModel):
    """discovery → discovery artifact."""

    discovery_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    discovered_at: datetime = Field(default_factory=_utcnow)
    videos: list[DiscoveredVideo] = Field(default_factory=list)
    #: Provider quota units consumed, when the provider reports them.
    quota_units_used: int | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# Analysis (intelligence plane)
# ---------------------------------------------------------------------------


class AnalysisPattern(ArtifactModel):
    """A content pattern observed across analyzed videos."""

    pattern: str = Field(min_length=1)
    supporting_video_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class AnalysisResult(ArtifactModel):
    """analysis → analysis artifact."""

    analysis_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    analyzed_at: datetime = Field(default_factory=_utcnow)
    video_count: int = Field(ge=0)
    total_views: int = Field(default=0, ge=0)
    average_views: float = Field(default=0.0, ge=0.0)
    top_performer_video_ids: list[str] = Field(default_factory=list)
    patterns: list[AnalysisPattern] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Opportunities (intelligence plane)
# ---------------------------------------------------------------------------


class OpportunityItem(ArtifactModel):
    """A single content opportunity candidate."""

    title: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=100.0)
    rationale: str = Field(min_length=1)
    content_gap: str | None = None


class OpportunityList(ArtifactModel):
    """opportunity → opportunity artifact."""

    opportunity_list_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=_utcnow)
    opportunities: list[OpportunityItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Research (research plane)
# ---------------------------------------------------------------------------


class ResearchSourceRef(ArtifactModel):
    source_id: str | None = None
    url: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, max_length=500)
    retrieved_at: datetime = Field(default_factory=_utcnow)


class ResearchClaimItem(ArtifactModel):
    claim: str = Field(min_length=1)
    support_status: Literal["supported", "contradicted", "unverified"] = "unverified"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_urls: list[str] = Field(default_factory=list)


class Contradiction(ArtifactModel):
    description: str = Field(min_length=1)
    claim_refs: list[str] = Field(default_factory=list)


class ResearchReport(ArtifactModel):
    """research → research artifact."""

    research_report_id: str = Field(min_length=1)
    opportunity_id: str | None = None
    topic: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=_utcnow)
    summary: str = Field(min_length=1)
    sources: list[ResearchSourceRef] = Field(default_factory=list)
    claims: list[ResearchClaimItem] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Content brief (content plane)
# ---------------------------------------------------------------------------


class ContentBrief(ArtifactModel):
    """brief → content brief artifact."""

    brief_id: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    angle: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    content_gaps_addressed: list[str] = Field(default_factory=list)
    estimated_duration_seconds: int = Field(ge=0)
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Script (content plane)
# ---------------------------------------------------------------------------


class Shot(ArtifactModel):
    """A single shot within a scene."""

    shot_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    shot_type: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    asset_requirements: list[str] = Field(default_factory=list)


class Scene(ArtifactModel):
    """A scene: narration plus its shots."""

    scene_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    heading: str | None = None
    narration: str = Field(min_length=1)
    visual_description: str | None = None
    duration_seconds: float = Field(default=0.0, ge=0.0)
    shots: list[Shot] = Field(default_factory=list)


class Script(ArtifactModel):
    """script → script artifact."""

    script_id: str = Field(min_length=1)
    brief_id: str | None = None
    title: str = Field(min_length=1)
    language: str = Field(default="en", min_length=2, max_length=16)
    scenes: list[Scene] = Field(default_factory=list)
    word_count: int = Field(default=0, ge=0)
    total_duration_seconds: float = Field(default=0.0, ge=0.0)


# ---------------------------------------------------------------------------
# Storyboard / visual plan (content plane)
# ---------------------------------------------------------------------------


class StoryboardShot(ArtifactModel):
    shot_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    shot_type: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    visual_prompt: str | None = None
    asset_hints: list[str] = Field(default_factory=list)


class StoryboardScene(ArtifactModel):
    scene_id: str = Field(min_length=1)
    narration_excerpt: str | None = None
    shots: list[StoryboardShot] = Field(default_factory=list)


class Storyboard(ArtifactModel):
    """visual planning → storyboard artifact."""

    storyboard_id: str = Field(min_length=1)
    script_id: str = Field(min_length=1)
    scenes: list[StoryboardScene] = Field(default_factory=list)
    total_duration_seconds: float = Field(default=0.0, ge=0.0)


# ---------------------------------------------------------------------------
# Production timeline (production plane)
# ---------------------------------------------------------------------------


class TimelineClip(ArtifactModel):
    clip_id: str = Field(min_length=1)
    start_seconds: float = Field(ge=0.0)
    duration_seconds: float = Field(ge=0.0)
    scene_id: str | None = None
    shot_id: str | None = None
    asset_id: str | None = None
    text: str | None = None


class TimelineTrack(ArtifactModel):
    track_type: Literal["video", "audio", "voice", "music", "subtitle", "graphic"]
    clips: list[TimelineClip] = Field(default_factory=list)


class ProductionTimeline(ArtifactModel):
    """production → timeline artifact."""

    timeline_id: str = Field(min_length=1)
    script_id: str | None = None
    tracks: list[TimelineTrack] = Field(default_factory=list)
    total_duration_seconds: float = Field(default=0.0, ge=0.0)


# ---------------------------------------------------------------------------
# QA (quality plane)
# ---------------------------------------------------------------------------


class QACheck(ArtifactModel):
    check_id: str = Field(min_length=1)
    category: Literal["factual", "narrative", "visual", "audio", "subtitle", "technical"]
    status: Literal["pass", "warn", "fail"]
    details: str | None = None


class QAReport(ArtifactModel):
    """QA → QA artifact."""

    report_id: str = Field(min_length=1)
    target_artifact_id: str = Field(min_length=1)
    target_artifact_type: str = Field(min_length=1, max_length=64)
    checked_at: datetime = Field(default_factory=_utcnow)
    checks: list[QACheck] = Field(default_factory=list)
    overall_status: Literal["pass", "warn", "fail"] = "pass"
    summary: str | None = None


# ---------------------------------------------------------------------------
# Publishing (publishing plane)
# ---------------------------------------------------------------------------


class ThumbnailConcept(ArtifactModel):
    description: str = Field(min_length=1)
    text_overlay: str | None = None
    style_notes: str | None = None


class PublishPackage(ArtifactModel):
    """publishing → publish package artifact (metadata + thumbnail concept)."""

    package_id: str = Field(min_length=1)
    opportunity_id: str | None = None
    title: str = Field(min_length=1, max_length=100)
    title_options: list[str] = Field(default_factory=list)
    description: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    language: str = Field(default="en", min_length=2, max_length=16)
    thumbnail_concept: ThumbnailConcept
    scheduled_at: datetime | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactContract:
    """Binds an artifact type to its model, schema version, and JSON Schema file."""

    artifact_type: ArtifactType
    schema_version: str
    model: type[ArtifactModel]
    schema_file: str


ARTIFACT_CONTRACTS: dict[ArtifactType, ArtifactContract] = {
    ArtifactType.DISCOVERY_RESULT: ArtifactContract(
        ArtifactType.DISCOVERY_RESULT,
        ARTIFACT_SCHEMA_VERSION,
        DiscoveryResult,
        "discovery_result.schema.json",
    ),
    ArtifactType.ANALYSIS_RESULT: ArtifactContract(
        ArtifactType.ANALYSIS_RESULT,
        ARTIFACT_SCHEMA_VERSION,
        AnalysisResult,
        "analysis_result.schema.json",
    ),
    ArtifactType.OPPORTUNITY_LIST: ArtifactContract(
        ArtifactType.OPPORTUNITY_LIST,
        ARTIFACT_SCHEMA_VERSION,
        OpportunityList,
        "opportunity_list.schema.json",
    ),
    ArtifactType.RESEARCH_REPORT: ArtifactContract(
        ArtifactType.RESEARCH_REPORT,
        ARTIFACT_SCHEMA_VERSION,
        ResearchReport,
        "research_report.schema.json",
    ),
    ArtifactType.CONTENT_BRIEF: ArtifactContract(
        ArtifactType.CONTENT_BRIEF,
        ARTIFACT_SCHEMA_VERSION,
        ContentBrief,
        "content_brief.schema.json",
    ),
    ArtifactType.SCRIPT: ArtifactContract(
        ArtifactType.SCRIPT, ARTIFACT_SCHEMA_VERSION, Script, "script.schema.json"
    ),
    ArtifactType.STORYBOARD: ArtifactContract(
        ArtifactType.STORYBOARD, ARTIFACT_SCHEMA_VERSION, Storyboard, "storyboard.schema.json"
    ),
    ArtifactType.PRODUCTION_TIMELINE: ArtifactContract(
        ArtifactType.PRODUCTION_TIMELINE,
        ARTIFACT_SCHEMA_VERSION,
        ProductionTimeline,
        "production_timeline.schema.json",
    ),
    ArtifactType.QA_REPORT: ArtifactContract(
        ArtifactType.QA_REPORT, ARTIFACT_SCHEMA_VERSION, QAReport, "qa_report.schema.json"
    ),
    ArtifactType.PUBLISH_PACKAGE: ArtifactContract(
        ArtifactType.PUBLISH_PACKAGE,
        ARTIFACT_SCHEMA_VERSION,
        PublishPackage,
        "publish_package.schema.json",
    ),
}


def validate_artifact_payload(
    artifact_type: ArtifactType | str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate ``payload`` against the contract for ``artifact_type``.

    Returns the normalized payload dict. Raises
    :class:`ArtifactValidationError` listing every issue on failure — invalid
    output is never silently accepted.
    """
    try:
        artifact_type = ArtifactType(artifact_type)
    except ValueError as exc:
        raise ArtifactValidationError(
            str(artifact_type), [f"unknown artifact type: {exc}"]
        ) from exc
    contract = ARTIFACT_CONTRACTS[artifact_type]
    try:
        model = contract.model.model_validate(payload)
    except ValidationError as exc:
        issues = [
            f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise ArtifactValidationError(artifact_type.value, issues) from exc
    normalized: dict[str, Any] = model.model_dump(mode="json")
    return normalized


def load_json_schema(artifact_type: ArtifactType | str, schemas_dir: Path | str) -> dict[str, Any]:
    """Load the published JSON Schema contract for an artifact type."""
    artifact_type = ArtifactType(artifact_type)
    contract = ARTIFACT_CONTRACTS[artifact_type]
    path = Path(schemas_dir) / contract.schema_file
    with path.open("r", encoding="utf-8") as handle:
        schema: dict[str, Any] = json.load(handle)
        return schema

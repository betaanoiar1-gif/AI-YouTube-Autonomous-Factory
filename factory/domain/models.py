"""Pydantic domain models for relational entities.

These models are the typed contracts used by services, the API layer, and
validation. They intentionally contain only fields justified by the current
architecture (see docs/domain-model.md) — no speculative fields.

Artifact-first entities (ContentBrief, Script, Scene, Shot, Timeline,
QAReport, and the other pipeline outputs) are defined in
:mod:`factory.schemas.artifacts` as versioned artifact contracts.

Typed jobs (DiscoveryJob, RenderJob, ...) are :class:`PipelineJob` rows with a
fixed ``type`` — see :mod:`factory.jobs.types`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Base config for domain models: strict about extra fields, UTC ISO times."""

    model_config = ConfigDict(extra="forbid")


class Niche(DomainModel):
    """A content niche."""

    id: str
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Project(DomainModel):
    """A content project: niche + language + audience + publishing frequency."""

    id: str
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    niche_id: str | None = None
    language: str = Field(default="en", min_length=2, max_length=16)
    target_audience: str | None = Field(default=None, max_length=200)
    publishing_frequency_per_week: int = Field(default=1, ge=1, le=100)
    status: Literal["active", "paused", "archived"] = "active"
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Channel(DomainModel):
    """A tracked YouTube channel."""

    id: str
    project_id: str | None = None
    external_channel_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=300)
    url: str | None = Field(default=None, max_length=500)
    status: Literal["active", "paused", "archived"] = "active"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Video(DomainModel):
    """A discovered YouTube video."""

    id: str
    channel_id: str
    external_video_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    description: str | None = None
    url: str | None = Field(default=None, max_length=500)
    duration_seconds: int | None = Field(default=None, ge=0)
    published_at: datetime | None = None
    language: str | None = Field(default=None, max_length=16)
    status: str = Field(default="discovered", max_length=32)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class VideoMetrics(DomainModel):
    """A point-in-time performance snapshot for a video."""

    id: str
    video_id: str
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    average_view_duration_seconds: float | None = Field(default=None, ge=0)
    click_through_rate: float | None = Field(default=None, ge=0, le=1)
    snapshot_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)


class ChannelMetrics(DomainModel):
    """A point-in-time performance snapshot for a channel."""

    id: str
    channel_id: str
    subscribers: int = Field(default=0, ge=0)
    total_views: int = Field(default=0, ge=0)
    total_videos: int = Field(default=0, ge=0)
    snapshot_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)


class TopicCluster(DomainModel):
    """A cluster of related topics."""

    id: str
    project_id: str | None = None
    label: str = Field(min_length=1, max_length=300)
    summary: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Topic(DomainModel):
    """A topic within a project (optionally assigned to a cluster)."""

    id: str
    project_id: str | None = None
    cluster_id: str | None = None
    name: str = Field(min_length=1, max_length=300)
    description: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ContentOpportunity(DomainModel):
    """A detected content opportunity with a success score."""

    id: str
    project_id: str
    topic_id: str | None = None
    cluster_id: str | None = None
    title: str = Field(min_length=1)
    angle: str | None = None
    content_gap: str | None = None
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    rationale: str | None = None
    status: Literal["candidate", "researched", "briefed", "scripted", "produced", "published"] = (
        "candidate"
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Source(DomainModel):
    """A research source."""

    id: str
    project_id: str | None = None
    opportunity_id: str | None = None
    url: str = Field(min_length=1, max_length=1000)
    source_type: str = Field(default="web", max_length=64)
    title: str | None = Field(default=None, max_length=500)
    collected_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ResearchDocument(DomainModel):
    """A collected research document."""

    id: str
    source_id: str
    opportunity_id: str | None = None
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ResearchClaim(DomainModel):
    """A single claim extracted from a research document."""

    id: str
    research_document_id: str
    claim: str = Field(min_length=1)
    support_status: Literal["supported", "contradicted", "unverified"] = "unverified"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Asset(DomainModel):
    """A media asset managed by the production plane."""

    id: str
    project_id: str | None = None
    type: Literal["image", "video", "audio", "music", "subtitle", "font", "document"]
    storage_ref: str = Field(min_length=1, max_length=1000)
    checksum: str = Field(min_length=1, max_length=64)
    byte_size: int = Field(default=0, ge=0)
    source: Literal["generated", "stock", "purchased", "recorded", "document"] = "generated"
    license_info: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProviderConfig(DomainModel):
    """A configured provider (non-secret configuration only)."""

    id: str
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["llm", "asr", "vision", "embedding", "image", "video", "tts", "discovery"]
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

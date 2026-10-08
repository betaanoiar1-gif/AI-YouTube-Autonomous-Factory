"""SQLAlchemy ORM models — the initial domain schema.

Design notes (see docs/domain-model.md):

* Every table has a string UUID primary key and ``created_at``/``updated_at``
  timestamps (UTC, timezone-aware).
* Enumerations are stored as plain strings; validation happens in the pydantic
  domain layer, which keeps migrations simple and portable.
* Entities that are primarily pipeline outputs (Script, Timeline, QAReport,
  ContentBrief, ...) are **artifact-first**: their canonical representation is
  a versioned artifact in the artifact store (``artifacts`` table + typed
  JSON Schema contracts). Lightweight relational rows link them to projects
  and opportunities where useful.
* Typed jobs (DiscoveryJob, RenderJob, ...) are realized as rows in the single
  ``pipeline_jobs`` table with a ``type`` discriminator — one resumable job
  system instead of N parallel ones (see docs/job-system.md).
* Referential integrity rule: FK constraints model ownership within a plane
  (project → channels/videos/metrics/topics/opportunities/sources/documents/
  claims/jobs/events, niche → projects, channel → videos/metrics, source →
  documents, document → claims, opportunity → briefs, cluster → topics).
  Cross-plane and observability references (``artifacts.project_id``,
  ``artifacts.job_id``, ``pipeline_jobs.*_artifact_id``,
  ``content_briefs.artifact_id``, ``provider_usage.job_id``,
  ``assets.project_id``) are plain indexed columns so historical records
  survive entity deletion and no circular FKs exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _uuid() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------


class Niche(Base, TimestampMixin):
    """A content niche (e.g. "Historical Mysteries")."""

    __tablename__ = "niches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)

    projects: Mapped[list[Project]] = relationship(back_populates="niche")


class Project(Base, TimestampMixin):
    """A content project: niche + language + audience + publishing frequency."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    niche_id: Mapped[str | None] = mapped_column(ForeignKey("niches.id"), nullable=True)
    language: Mapped[str] = mapped_column(String(16), default="en")
    target_audience: Mapped[str | None] = mapped_column(String(200), nullable=True)
    publishing_frequency_per_week: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    #: Per-project configuration (project configuration layer) — never secrets.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    niche: Mapped[Niche | None] = relationship(back_populates="projects")
    channels: Mapped[list[Channel]] = relationship(back_populates="project")
    jobs: Mapped[list[PipelineJob]] = relationship(back_populates="project")
    topics: Mapped[list[Topic]] = relationship(back_populates="project")
    opportunities: Mapped[list[ContentOpportunity]] = relationship(back_populates="project")


class Channel(Base, TimestampMixin):
    """A YouTube channel tracked by a project."""

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    external_channel_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    project: Mapped[Project | None] = relationship(back_populates="channels")
    videos: Mapped[list[Video]] = relationship(back_populates="channel")
    metrics: Mapped[list[ChannelMetrics]] = relationship(back_populates="channel")


# ---------------------------------------------------------------------------
# Intelligence plane
# ---------------------------------------------------------------------------


class Video(Base, TimestampMixin):
    """A discovered YouTube video."""

    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    external_video_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="discovered", index=True)

    channel: Mapped[Channel] = relationship(back_populates="videos")
    metrics: Mapped[list[VideoMetrics]] = relationship(back_populates="video")


class VideoMetrics(Base):
    """A point-in-time performance snapshot for a video."""

    __tablename__ = "video_metrics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    average_view_duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    click_through_rate: Mapped[float | None] = mapped_column(nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    video: Mapped[Video] = relationship(back_populates="metrics")

    __table_args__ = (Index("ix_video_metrics_video_snapshot", "video_id", "snapshot_at"),)


class ChannelMetrics(Base):
    """A point-in-time performance snapshot for a channel."""

    __tablename__ = "channel_metrics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    subscribers: Mapped[int] = mapped_column(Integer, default=0)
    total_views: Mapped[int] = mapped_column(Integer, default=0)
    total_videos: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    channel: Mapped[Channel] = relationship(back_populates="metrics")


class TopicCluster(Base, TimestampMixin):
    """A cluster of related topics discovered in the niche."""

    __tablename__ = "topic_clusters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    label: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    topics: Mapped[list[Topic]] = relationship(back_populates="cluster")


class Topic(Base, TimestampMixin):
    """A topic within a project (optionally assigned to a cluster)."""

    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(ForeignKey("topic_clusters.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project | None] = relationship(back_populates="topics")
    cluster: Mapped[TopicCluster | None] = relationship(back_populates="topics")


class ContentOpportunity(Base, TimestampMixin):
    """A detected content opportunity (with a success score and rationale)."""

    __tablename__ = "content_opportunities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"), nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(ForeignKey("topic_clusters.id"), nullable=True)
    title: Mapped[str] = mapped_column(Text)
    angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_gap: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float] = mapped_column(default=0.0, index=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )  # candidate/researched/briefed/scripted/produced/published

    project: Mapped[Project] = relationship(back_populates="opportunities")
    briefs: Mapped[list[ContentBriefRow]] = relationship(back_populates="opportunity")


# ---------------------------------------------------------------------------
# Research plane
# ---------------------------------------------------------------------------


class Source(Base, TimestampMixin):
    """A research source collected for a project/opportunity."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_opportunities.id"), nullable=True
    )
    url: Mapped[str] = mapped_column(String(1000))
    source_type: Mapped[str] = mapped_column(String(64), default="web")
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    documents: Mapped[list[ResearchDocument]] = relationship(back_populates="source")


class ResearchDocument(Base, TimestampMixin):
    """A collected research document (extracted content from a source)."""

    __tablename__ = "research_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_opportunities.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)

    source: Mapped[Source] = relationship(back_populates="documents")
    claims: Mapped[list[ResearchClaim]] = relationship(back_populates="document")


class ResearchClaim(Base, TimestampMixin):
    """A single claim extracted from a research document."""

    __tablename__ = "research_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    research_document_id: Mapped[str] = mapped_column(
        ForeignKey("research_documents.id"), index=True
    )
    claim: Mapped[str] = mapped_column(Text)
    support_status: Mapped[str] = mapped_column(
        String(32), default="unverified"
    )  # supported/contradicted/unverified
    confidence: Mapped[float] = mapped_column(default=0.0)

    document: Mapped[ResearchDocument] = relationship(back_populates="claims")


# ---------------------------------------------------------------------------
# Content plane
# ---------------------------------------------------------------------------


class ContentBriefRow(Base, TimestampMixin):
    """Relational link row for a content brief.

    The brief's canonical content is a versioned ``content_brief`` artifact;
    this row links it to its opportunity and records workflow status.
    """

    __tablename__ = "content_briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("content_opportunities.id"), unique=True, index=True
    )
    # Logical reference (no FK): points at a versioned artifact in the store.
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)

    opportunity: Mapped[ContentOpportunity] = relationship(back_populates="briefs")


# ---------------------------------------------------------------------------
# Production plane
# ---------------------------------------------------------------------------


class Asset(Base, TimestampMixin):
    """A media asset managed by the production plane.

    Binary content lives in the artifact/asset store; this row tracks
    provenance, licensing, and integrity. ``source`` is one of
    ``generated``/``stock``/``purchased``/``recorded``/``document``.
    """

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Logical reference (no FK): asset rows index the asset store and
    # outlive project rows (see the referential-integrity rule above).
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # image/video/audio/music/subtitle/font/document
    type: Mapped[str] = mapped_column(String(32), index=True)
    storage_ref: Mapped[str] = mapped_column(String(1000))
    checksum: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32), default="generated", index=True)
    license_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    __table_args__ = (Index("ix_assets_type_source", "type", "source"),)


# ---------------------------------------------------------------------------
# Artifact system
# ---------------------------------------------------------------------------


class Artifact(Base, TimestampMixin):
    """A versioned pipeline artifact record.

    The payload lives in the artifact store (filesystem); ``storage_ref`` is a
    relative path inside the configured artifact root. Artifacts are immutable:
    a new version is a new row with a new id and a new storage path — nothing
    is ever silently overwritten.
    """

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Logical references (no FK): artifacts are immutable files indexed here;
    # they outlive the jobs that produced them.
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lineage_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    storage_ref: Mapped[str] = mapped_column(String(1000))
    checksum: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    __table_args__ = (
        Index("ix_artifacts_lineage", "project_id", "type", "lineage_key"),
        Index("ix_artifacts_type_created", "type", "created_at"),
    )


# ---------------------------------------------------------------------------
# Job system
# ---------------------------------------------------------------------------


class PipelineJob(Base, TimestampMixin):
    """A resumable pipeline job (single table for all job types).

    Typed jobs (DiscoveryJob, AnalysisJob, ResearchJob, ScriptJob, RenderJob,
    QAJob, ...) are rows here with a ``type`` discriminator — see
    docs/job-system.md.
    """

    __tablename__ = "pipeline_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    #: Job input configuration (never secrets).
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Resumable checkpoint written by the handler between attempts.
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Logical references (no FK): input/output artifacts in the artifact store.
    input_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    output_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project | None] = relationship(back_populates="jobs")
    events: Mapped[list[JobEvent]] = relationship(back_populates="job")

    __table_args__ = (Index("ix_pipeline_jobs_status_type", "status", "type"),)


class JobEvent(Base):
    """A durable, structured log event attached to a job."""

    __tablename__ = "job_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("pipeline_jobs.id"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    job: Mapped[PipelineJob] = relationship(back_populates="events")


# ---------------------------------------------------------------------------
# Providers & usage
# ---------------------------------------------------------------------------


class Provider(Base, TimestampMixin):
    """A configured provider (non-secret configuration only)."""

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # llm/asr/vision/embedding/image/video/tts/discovery
    kind: Mapped[str] = mapped_column(String(32), index=True)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    #: Non-secret provider configuration (endpoints, model defaults, limits).
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProviderUsage(Base):
    """One observed provider request (cost & observability record).

    Token fields are NULL when the provider did not report usage — we record
    that usage was unavailable rather than estimating. Never stores API keys.
    """

    __tablename__ = "provider_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)
    purpose: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # Logical reference (no FK): usage history must survive job deletion.
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(default=True, index=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    usage_available: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (Index("ix_provider_usage_job", "job_id", "requested_at"),)


class LLMCacheEntry(Base):
    """A cached LLM response (deduplication of deterministic requests)."""

    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)
    response_json: Mapped[str] = mapped_column(Text)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

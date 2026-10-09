"""Domain models (pydantic) for the factory's core entities.

See docs/domain-model.md. Relational entities are modeled here; entities that
are primarily pipeline outputs (ContentBrief, Script, Scene, Shot, Timeline,
QAReport) are artifact-first — their canonical models live in
:mod:`factory.schemas.artifacts` as typed artifact contracts.
"""

from factory.domain.models import (
    Asset,
    Channel,
    ChannelMetrics,
    ContentOpportunity,
    Niche,
    Project,
    ProviderConfig,
    ResearchClaim,
    ResearchDocument,
    Source,
    Topic,
    TopicCluster,
    Video,
    VideoMetrics,
)

__all__ = [
    "Asset",
    "Channel",
    "ChannelMetrics",
    "ContentOpportunity",
    "Niche",
    "Project",
    "ProviderConfig",
    "ResearchClaim",
    "ResearchDocument",
    "Source",
    "Topic",
    "TopicCluster",
    "Video",
    "VideoMetrics",
]

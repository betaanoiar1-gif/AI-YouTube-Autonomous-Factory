"""Domain model tests: entities construct, validate, and reject extra fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factory.domain.models import (
    Asset,
    Channel,
    ContentOpportunity,
    Niche,
    Project,
    ProviderConfig,
    ResearchClaim,
    ResearchDocument,
    Source,
    Topic,
    Video,
    VideoMetrics,
)


def test_project_defaults():
    project = Project(id="p1", name="Historical Mysteries Factory")
    assert project.language == "en"
    assert project.publishing_frequency_per_week == 1
    assert project.status == "active"
    assert project.config == {}


def test_project_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Project.model_validate({"id": "p1", "name": "x", "unexpected": True})


def test_niche():
    niche = Niche(id="n1", name="Historical Mysteries", keywords=["history", "mystery"])
    assert niche.keywords == ["history", "mystery"]


def test_channel_requires_external_id():
    with pytest.raises(ValidationError):
        Channel(id="c1", external_channel_id="", name="chan")
    channel = Channel(id="c1", external_channel_id="UC123", name="chan")
    assert channel.status == "active"


def test_video_and_metrics():
    video = Video(id="v1", channel_id="c1", external_video_id="vid1", title="T")
    assert video.duration_seconds is None
    metrics = VideoMetrics(id="m1", video_id="v1", views=10, likes=2, comments=1)
    assert metrics.click_through_rate is None
    with pytest.raises(ValidationError):
        VideoMetrics(id="m2", video_id="v1", click_through_rate=1.5)  # > 1 rejected


def test_opportunity_score_bounds():
    opp = ContentOpportunity(id="o1", project_id="p1", title="T", score=87.5)
    assert opp.status == "candidate"
    with pytest.raises(ValidationError):
        ContentOpportunity(id="o2", project_id="p1", title="T", score=101)


def test_research_entities():
    source = Source(id="s1", url="https://example.com/x")
    assert source.source_type == "web"
    doc = ResearchDocument(id="d1", source_id="s1", title="t", content="body")
    assert doc.opportunity_id is None
    claim = ResearchClaim(
        id="c1", research_document_id="d1", claim="The sky is blue", confidence=0.9
    )
    assert claim.support_status == "unverified"
    with pytest.raises(ValidationError):
        ResearchClaim(id="c2", research_document_id="d1", claim="x", support_status="maybe")


def test_topic_and_asset():
    topic = Topic(id="t1", project_id="p1", name="Topic")
    asset = Asset(
        id="a1",
        type="image",
        storage_ref="p1/image/a1.png",
        checksum="abc123",
        source="generated",
    )
    assert topic.cluster_id is None
    assert asset.metadata == {}


def test_provider_config():
    provider = ProviderConfig(id="pr1", name="cleanapis", kind="llm")
    assert provider.enabled is True
    with pytest.raises(ValidationError):
        ProviderConfig(id="pr2", name="x", kind="not-a-kind")

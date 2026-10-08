"""Normalized, vendor-neutral discovery request/response models.

These shapes are what intelligence-plane business logic sees. A provider
implementation (e.g. the YouTube Data API v3 provider) translates between
these models and its own wire format.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

#: Orders supported by the YouTube Data API v3 ``search`` endpoint.
SEARCH_ORDERS = ("date", "rating", "relevance", "viewCount", "title")

#: Video duration filters supported by the YouTube Data API v3 ``search``.
VIDEO_DURATIONS = ("any", "short", "medium", "long")


class DiscoveryQuery(BaseModel):
    """A normalized discovery search query."""

    q: str = Field(min_length=1, max_length=500)
    relevance_language: str | None = Field(default=None, max_length=8)
    region_code: str | None = Field(default=None, max_length=4)
    published_after: datetime | None = None
    published_before: datetime | None = None
    order: str = Field(default="relevance")
    video_duration: str | None = Field(default=None)
    max_results_per_page: int = Field(default=25, ge=1, le=50)
    page_token: str | None = Field(default=None, max_length=256)

    def api_params(self) -> dict[str, Any]:
        """Query parameters for the documented ``search`` list endpoint."""
        params: dict[str, Any] = {
            "part": "snippet",
            "q": self.q,
            "type": "video",
            "maxResults": self.max_results_per_page,
            "order": self.order,
        }
        if self.relevance_language:
            params["relevanceLanguage"] = self.relevance_language
        if self.region_code:
            params["regionCode"] = self.region_code
        if self.published_after is not None:
            params["publishedAfter"] = self.published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.published_before is not None:
            params["publishedBefore"] = self.published_before.strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.video_duration and self.video_duration != "any":
            params["videoDuration"] = self.video_duration
        if self.page_token:
            params["pageToken"] = self.page_token
        return params


class DiscoveredVideoItem(BaseModel):
    """A normalized discovered video with a metrics snapshot.

    Only metadata is stored — never full competitor content (descriptions are
    reduced to a character count).
    """

    video_id: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1, max_length=500)
    published_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    channel_title: str | None = Field(default=None, max_length=256)
    description_chars: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list)
    category_id: str | None = Field(default=None, max_length=16)
    definition: str | None = Field(default=None, max_length=8)


class DiscoveredChannelItem(BaseModel):
    """A normalized discovered channel with a metrics snapshot."""

    channel_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    subscriber_count: int | None = Field(default=None, ge=0)
    view_count: int | None = Field(default=None, ge=0)
    video_count: int | None = Field(default=None, ge=0)


class DiscoveryPage(BaseModel):
    """One page of discovery results (pagination-aware)."""

    items: list[DiscoveredVideoItem] = Field(default_factory=list)
    next_page_token: str | None = Field(default=None, max_length=256)
    total_results: int | None = Field(default=None, ge=0)
    #: Documented quota units consumed by the API call(s) behind this page.
    quota_units_used: int = Field(default=0, ge=0)

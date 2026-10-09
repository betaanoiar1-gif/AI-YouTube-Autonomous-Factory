"""Discovery engine (YOUTUBE_DISCOVERY job).

Runs the first production pipeline stage: project/niche + language + target
audience + configurable search parameters + result limit → a versioned
``discovery_result`` artifact with normalized video/channel data.

Resumability (through the existing job system): the job checkpoint carries the
pipeline stage, the collected videos, the next page token, and the quota units
used, so a retried job continues where it stopped instead of re-spending quota
on completed pages. Video/channel details are additionally cached by the
provider, so re-runs of completed lookups never call the API again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from factory.config.youtube_config import YouTubeSettings
from factory.jobs.runner import JobContext
from factory.jobs.types import JobRecord
from factory.providers.base import DiscoveryProvider
from factory.providers.discovery.types import DiscoveryQuery
from factory.schemas.artifacts import ArtifactType
from factory.storage.artifacts import ArtifactStore

#: Checkpoint stages.
STAGE_SEARCH = "search"
STAGE_VIDEO_METRICS = "video_metrics"
STAGE_CHANNEL_METRICS = "channel_metrics"

_STAGES = (STAGE_SEARCH, STAGE_VIDEO_METRICS, STAGE_CHANNEL_METRICS)


class DiscoveryJobConfig:
    """Validated input configuration for a discovery job (from the job payload)."""

    def __init__(
        self, payload: dict[str, Any], *, project_id: str, settings: YouTubeSettings
    ) -> None:
        self.project_id = project_id
        query = payload.get("query") or payload.get("q")
        if isinstance(payload.get("keywords"), list) and payload["keywords"]:
            query = query or " ".join(str(k) for k in payload["keywords"])
        if not isinstance(query, str) or not query.strip():
            raise ValueError("discovery job payload requires a 'query' (or 'keywords')")
        self.query = query.strip()
        self.language = payload.get("language") or payload.get("relevance_language")
        self.target_audience = payload.get("target_audience")
        self.region_code = payload.get("region_code")
        self.order = payload.get("order") or "relevance"
        self.video_duration = payload.get("video_duration")
        self.published_after = payload.get("published_after")
        self.published_before = payload.get("published_before")
        limit = payload.get("result_limit", settings.discovery_result_limit)
        self.result_limit = max(1, min(int(limit), settings.discovery_result_limit * 10))
        page_size = payload.get("page_size", settings.search_max_results_per_page)
        self.page_size = max(1, min(int(page_size), 50))

    def query_model(self) -> DiscoveryQuery:
        """The normalized provider query for this job."""
        published_after = _parse_datetime(self.published_after)
        published_before = _parse_datetime(self.published_before)
        return DiscoveryQuery(
            q=self.query,
            relevance_language=self.language if isinstance(self.language, str) else None,
            region_code=self.region_code if isinstance(self.region_code, str) else None,
            published_after=published_after,
            published_before=published_before,
            order=self.order if isinstance(self.order, str) else "relevance",
            video_duration=self.video_duration if isinstance(self.video_duration, str) else None,
            max_results_per_page=self.page_size,
        )

    def search_parameters(self) -> dict[str, Any]:
        """The search parameters recorded in the artifact."""
        return {
            "q": self.query,
            "order": self.order,
            "relevance_language": self.language,
            "region_code": self.region_code,
            "video_duration": self.video_duration,
            "published_after": self.published_after,
            "published_before": self.published_before,
            "max_results_per_page": self.page_size,
            "result_limit": self.result_limit,
        }


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            from datetime import UTC

            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


class DiscoveryEngine:
    """Runs discovery through a DiscoveryProvider and stores the artifact."""

    def __init__(
        self,
        provider: DiscoveryProvider,
        artifact_store: ArtifactStore,
        *,
        settings: YouTubeSettings,
    ) -> None:
        self._provider = provider
        self._artifact_store = artifact_store
        self._settings = settings

    def run(self, job: JobRecord, context: JobContext) -> str:
        """Run the discovery job; returns the output artifact id."""
        config = DiscoveryJobConfig(
            job.payload or {}, project_id=job.project_id or "", settings=self._settings
        )
        if not job.project_id:
            raise ValueError("discovery job requires a project_id")

        checkpoint: dict[str, Any] = dict(context.checkpoint or {})
        stage = checkpoint.get("stage", STAGE_SEARCH)
        if stage not in _STAGES:
            stage = STAGE_SEARCH
        collected: dict[str, dict[str, Any]] = {
            video["video_id"]: video for video in checkpoint.get("videos", [])
        }
        quota_used: int = int(checkpoint.get("quota_units_used", 0))
        next_page_token: str | None = checkpoint.get("next_page_token")

        # --- stage 1: search (paginated, resumable) ---
        if stage == STAGE_SEARCH:
            query = config.query_model()
            while True:
                remaining = config.result_limit - len(collected)
                if remaining <= 0:
                    break
                page = self._provider.discover_videos(
                    query,
                    max_results=min(config.page_size, remaining),
                    job_id=job.id,
                    page_token=next_page_token,
                    max_pages=1,
                )
                quota_used += page.quota_units_used
                for item in page.items:
                    if item.video_id not in collected:
                        collected[item.video_id] = item.model_dump(mode="json")
                next_page_token = page.next_page_token
                context.set_progress(self._progress(len(collected), config.result_limit, stage))
                context.save_checkpoint(
                    {
                        "stage": STAGE_SEARCH,
                        "videos": list(collected.values()),
                        "quota_units_used": quota_used,
                        "next_page_token": next_page_token,
                    }
                )
                if len(collected) >= config.result_limit or not next_page_token:
                    break
            stage = STAGE_VIDEO_METRICS
            checkpoint = {
                "stage": stage,
                "videos": list(collected.values()),
                "quota_units_used": quota_used,
                "next_page_token": None,
            }
            context.save_checkpoint(checkpoint)

        # --- stage 2: video metrics (cached by the provider) ---
        if stage == STAGE_VIDEO_METRICS:
            video_ids = list(collected.keys())
            video_items = self._provider.get_video_metrics(video_ids, job_id=job.id)
            quota_used = max(quota_used, self._provider.quota_used() or quota_used)
            for item in video_items:
                if item.video_id in collected:
                    collected[item.video_id].update(item.model_dump(mode="json"))
            context.set_progress(self._progress(len(video_ids), len(video_ids), stage))
            checkpoint = {
                "stage": STAGE_CHANNEL_METRICS,
                "videos": list(collected.values()),
                "quota_units_used": quota_used,
            }
            context.save_checkpoint(checkpoint)
            stage = STAGE_CHANNEL_METRICS

        # --- stage 3: channel metrics ---
        channels: list[dict[str, Any]] = []
        if stage == STAGE_CHANNEL_METRICS:
            channel_ids = sorted({v["channel_id"] for v in collected.values()})
            channel_items = self._provider.get_channel_metrics(channel_ids, job_id=job.id)
            quota_used = max(quota_used, self._provider.quota_used() or quota_used)
            from datetime import UTC, datetime

            for channel_item in channel_items:
                payload = channel_item.model_dump(mode="json")
                payload["retrieved_at"] = datetime.now(UTC).isoformat()
                channels.append(payload)
            context.set_progress(100)

        # --- artifact ---
        payload = {
            "discovery_id": job.id,
            "project_id": config.project_id,
            "query": config.query,
            "language": config.language if isinstance(config.language, str) else None,
            "target_audience": config.target_audience
            if isinstance(config.target_audience, str)
            else None,
            "search_parameters": config.search_parameters(),
            "videos": [collected[video_id] for video_id in sorted(collected.keys())],
            "channels": channels,
            "quota_units_used": quota_used,
            "provider": {
                "name": self._provider.name,
                "quota_units_used": quota_used,
                "result_limit": config.result_limit,
                "video_count": len(collected),
                "channel_count": len(channels),
            },
        }
        record = self._artifact_store.save(
            ArtifactType.DISCOVERY_RESULT,
            config.project_id,
            payload,
            job_id=job.id,
            metadata={"job_type": job.type.value, "query": config.query},
        )
        return record.id

    @staticmethod
    def _progress(done: int, total: int, stage: str) -> int:
        """Stage-weighted progress: search 0-60, video metrics 60-85, channels 85-100."""
        if total <= 0:
            return 0
        ratio = min(done / total, 1.0)
        if stage == STAGE_SEARCH:
            return int(ratio * 60)
        if stage == STAGE_VIDEO_METRICS:
            return 60 + int(ratio * 25)
        return 85 + int(ratio * 15)

"""Provider usage tracking.

Every real provider request made by the application is recorded here:

* provider, model, purpose, job id
* request timestamp and latency
* token usage (input/output/total) **when the provider exposes it** — when it
  does not, the fields are stored as NULL and the record notes that usage was
  unavailable; we never silently estimate tokens.
* success/failure and error type

The API key is NEVER recorded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from factory.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ProviderUsageRecord:
    """A single provider request observation. Never contains secrets."""

    provider: str
    model: str
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    success: bool = True
    error_type: str | None = None
    job_id: str | None = None
    purpose: str | None = None
    request_id: str | None = None
    usage_available: bool = True
    #: Provider-specific metadata (e.g. quota units for the YouTube API).
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["requested_at"] = self.requested_at.isoformat()
        return data


class UsageTracker(Protocol):
    """Sink for provider usage records."""

    def record(self, record: ProviderUsageRecord) -> None: ...


class SQLAlchemyUsageTracker:
    """Persist usage records to the ``provider_usage`` table."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record(self, record: ProviderUsageRecord) -> None:
        from factory.storage.models import ProviderUsage  # local import: avoid cycles

        with self._session_factory() as session:
            row = ProviderUsage(
                provider=record.provider,
                model=record.model,
                purpose=record.purpose,
                job_id=record.job_id,
                requested_at=record.requested_at,
                latency_ms=record.latency_ms,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                total_tokens=record.total_tokens,
                success=record.success,
                error_type=record.error_type,
                request_id=record.request_id,
                usage_available=record.usage_available,
                provider_metadata=record.metadata,
            )
            session.add(row)
            session.commit()


class LoggingUsageTracker:
    """Fallback tracker: emits a structured log line (no database required)."""

    def record(self, record: ProviderUsageRecord) -> None:
        logger.info(
            "provider_usage",
            extra={
                "provider": record.provider,
                "model": record.model,
                "purpose": record.purpose,
                "job_id": record.job_id,
                "latency_ms": record.latency_ms,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
                "total_tokens": record.total_tokens,
                "success": record.success,
                "error_type": record.error_type,
                "usage_available": record.usage_available,
            },
        )


class CompositeUsageTracker:
    """Fan out records to multiple trackers."""

    def __init__(self, trackers: list[UsageTracker]) -> None:
        self._trackers = trackers

    def record(self, record: ProviderUsageRecord) -> None:
        for tracker in self._trackers:
            try:
                tracker.record(record)
            except Exception:
                logger.exception("usage_tracker_failed", extra={"tracker": type(tracker).__name__})

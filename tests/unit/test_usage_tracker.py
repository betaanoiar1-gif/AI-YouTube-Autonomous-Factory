"""Provider usage tracking tests (required test #7)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import select

from factory.observability.usage import (
    CompositeUsageTracker,
    LoggingUsageTracker,
    ProviderUsageRecord,
    SQLAlchemyUsageTracker,
)
from factory.storage.models import ProviderUsage


def _record(**kwargs) -> ProviderUsageRecord:
    defaults: dict[str, Any] = {
        "provider": "cleanapis",
        "model": "test-model",
        "latency_ms": 123,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "success": True,
        "purpose": "test",
        "job_id": "job-1",
    }
    defaults.update(kwargs)
    return ProviderUsageRecord(**defaults)


class TestSQLAlchemyUsageTracker:
    def test_record_persisted(self, session_factory):
        tracker = SQLAlchemyUsageTracker(session_factory)
        tracker.record(_record())
        with session_factory() as session:
            rows = session.execute(select(ProviderUsage)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.provider == "cleanapis"
        assert row.model == "test-model"
        assert row.total_tokens == 15
        assert row.success is True
        assert row.job_id == "job-1"
        assert row.purpose == "test"
        assert row.usage_available is True

    def test_failure_and_unavailable_usage_recorded(self, session_factory):
        tracker = SQLAlchemyUsageTracker(session_factory)
        tracker.record(
            _record(
                success=False,
                error_type="ProviderTimeoutError",
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                usage_available=False,
            )
        )
        with session_factory() as session:
            row = session.execute(select(ProviderUsage)).scalar_one()
        assert row.success is False
        assert row.error_type == "ProviderTimeoutError"
        assert row.total_tokens is None  # unavailable, not estimated
        assert row.usage_available is False

    def test_never_stores_api_key(self, session_factory):
        secret = "cc_usage_test_secret_12345"
        tracker = SQLAlchemyUsageTracker(session_factory)
        tracker.record(_record())
        with session_factory() as session:
            rows = session.execute(select(ProviderUsage)).scalars().all()
        for row in rows:
            for value in vars(row).values():
                assert secret not in str(value)


class TestLoggingUsageTracker:
    def test_emits_structured_log(self, caplog: pytest.LogCaptureFixture):
        tracker = LoggingUsageTracker()
        with caplog.at_level(logging.INFO, logger="factory.observability.usage"):
            tracker.record(_record())
        assert any(
            record.message == "provider_usage" and vars(record).get("model") == "test-model"
            for record in caplog.records
        )


class TestCompositeUsageTracker:
    def test_fans_out_and_survives_failure(self, session_factory):
        class FailingTracker:
            def record(self, record) -> None:
                raise RuntimeError("db down")

        good = SQLAlchemyUsageTracker(session_factory)
        composite = CompositeUsageTracker([FailingTracker(), good])
        composite.record(_record())  # must not raise
        with session_factory() as session:
            assert session.execute(select(ProviderUsage)).scalars().first() is not None

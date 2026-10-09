"""Budget enforcement tests."""

from __future__ import annotations

import pytest

from factory.errors import BudgetExceededError
from factory.providers.budget import (
    BudgetEnforcer,
    InMemoryBudgetStore,
    RequestBudget,
    SQLAlchemyBudgetStore,
)


class TestInMemoryBudgetStore:
    def test_counts_requests_and_tokens(self):
        store = InMemoryBudgetStore()
        store.add("j1", provider="cleanapis", total_tokens=100)
        store.add("j1", provider="cleanapis", total_tokens=50)
        store.add("j1", provider="cleanapis", total_tokens=None)  # unavailable usage
        count, tokens = store.get_spend("j1", provider="cleanapis")
        assert count == 3
        assert tokens == 150

    def test_isolation_between_jobs_and_providers(self):
        store = InMemoryBudgetStore()
        store.add("j1", provider="cleanapis", total_tokens=10)
        store.add("j2", provider="cleanapis", total_tokens=10)
        store.add("j1", provider="other", total_tokens=10)
        assert store.get_spend("j1", provider="cleanapis") == (1, 10)
        assert store.get_spend("j2", provider="cleanapis") == (1, 10)
        assert store.get_spend("j1", provider="other") == (1, 10)


class TestBudgetEnforcer:
    def test_under_budget_passes(self):
        store = InMemoryBudgetStore()
        store.add("j1", provider="cleanapis", total_tokens=10)
        enforcer = BudgetEnforcer(store, RequestBudget(max_requests=5, max_total_tokens=1000))
        enforcer.check(job_id="j1", provider="cleanapis")  # no raise

    def test_request_limit_enforced(self):
        store = InMemoryBudgetStore()
        for _ in range(5):
            store.add("j1", provider="cleanapis", total_tokens=1)
        enforcer = BudgetEnforcer(store, RequestBudget(max_requests=5))
        with pytest.raises(BudgetExceededError):
            enforcer.check(job_id="j1", provider="cleanapis")

    def test_token_limit_enforced(self):
        store = InMemoryBudgetStore()
        store.add("j1", provider="cleanapis", total_tokens=2000)
        enforcer = BudgetEnforcer(store, RequestBudget(max_requests=5, max_total_tokens=1000))
        with pytest.raises(BudgetExceededError):
            enforcer.check(job_id="j1", provider="cleanapis")

    def test_unavailable_tokens_do_not_block(self):
        store = InMemoryBudgetStore()
        store.add("j1", provider="cleanapis", total_tokens=None)
        enforcer = BudgetEnforcer(store, RequestBudget(max_requests=5, max_total_tokens=1000))
        enforcer.check(job_id="j1", provider="cleanapis")  # no raise


class TestSQLAlchemyBudgetStore:
    def test_computes_spend_from_usage_table(self, session_factory):
        from factory.observability.usage import ProviderUsageRecord, SQLAlchemyUsageTracker

        tracker = SQLAlchemyUsageTracker(session_factory)
        for _ in range(2):
            tracker.record(
                ProviderUsageRecord(provider="cleanapis", model="m", total_tokens=100, job_id="j1")
            )
        tracker.record(
            ProviderUsageRecord(provider="cleanapis", model="m", total_tokens=50, job_id="j2")
        )
        store = SQLAlchemyBudgetStore(session_factory)
        assert store.get_spend("j1", provider="cleanapis") == (2, 200)
        assert store.get_spend("j2", provider="cleanapis") == (1, 50)
        assert store.get_spend("unknown", provider="cleanapis") == (0, None)

"""Request budgets (cost control foundation).

Budgets are enforced per job before any provider call is made. Spending is
computed from the persisted ``provider_usage`` table, so budgets survive
restarts and remain auditable. When no database is available, an in-memory
store is used (documented limitation: not shared across processes).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from factory.errors import BudgetExceededError


@dataclass
class RequestBudget:
    """Per-job budget limits."""

    max_requests: int
    max_total_tokens: int | None = None


class BudgetStore(Protocol):
    def get_spend(self, job_id: str, *, provider: str) -> tuple[int, int | None]:
        """Return (request_count, total_tokens) spent for a job on a provider."""


class SQLAlchemyBudgetStore:
    """Computes spend from the provider_usage table (source of truth)."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get_spend(self, job_id: str, *, provider: str) -> tuple[int, int | None]:
        from sqlalchemy import func, select

        from factory.storage.models import ProviderUsage

        with self._session_factory() as session:
            stmt = select(
                func.count(ProviderUsage.id),
                func.sum(ProviderUsage.total_tokens),
            ).where(
                ProviderUsage.job_id == job_id,
                ProviderUsage.provider == provider,
            )
            row = session.execute(stmt).one()
            count = int(row[0] or 0)
            tokens = row[1]
            return count, (int(tokens) if tokens is not None else None)


class InMemoryBudgetStore:
    """Process-local spend tracking (tests / no-database operation)."""

    def __init__(self) -> None:
        self._spend: dict[tuple[str, str], list[int | None]] = {}

    def add(self, job_id: str, *, provider: str, total_tokens: int | None) -> None:
        self._spend.setdefault((job_id, provider), []).append(total_tokens)

    def get_spend(self, job_id: str, *, provider: str) -> tuple[int, int | None]:
        entries = self._spend.get((job_id, provider), [])
        tokens = [t for t in entries if t is not None]
        return len(entries), (sum(tokens) if tokens else None)


class BudgetEnforcer:
    """Enforce a :class:`RequestBudget` for a job before provider calls."""

    def __init__(self, store: BudgetStore, budget: RequestBudget) -> None:
        self._store = store
        self._budget = budget

    def check(self, *, job_id: str, provider: str) -> None:
        """Raise :class:`BudgetExceededError` if the job is over budget."""
        requests, tokens = self._store.get_spend(job_id, provider=provider)
        if requests >= self._budget.max_requests:
            raise BudgetExceededError(
                f"Job {job_id} exceeded its request budget "
                f"({requests} >= {self._budget.max_requests} requests on provider {provider!r})",
                provider=provider,
                details={
                    "job_id": job_id,
                    "requests": requests,
                    "limit": self._budget.max_requests,
                },
            )
        token_limit = self._budget.max_total_tokens
        if token_limit is not None and tokens is not None and tokens >= token_limit:
            raise BudgetExceededError(
                f"Job {job_id} exceeded its token budget "
                f"({tokens} >= {token_limit} tokens on provider {provider!r})",
                provider=provider,
                details={
                    "job_id": job_id,
                    "total_tokens": tokens,
                    "limit": token_limit,
                },
            )

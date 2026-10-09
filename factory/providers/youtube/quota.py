"""Quota accounting for the YouTube Data API v3.

Unit costs are the DOCUMENTED per-call costs (developers.google.com/youtube/v3/
determine_quota_cost): ``search`` = 100 units, ``videos`` = 1, ``channels`` = 1.
Quota values are never invented — only documented costs are counted, and the
API does not expose remaining quota, so the tracker enforces a configurable
per-run budget conservatively (units are charged before each call, including
calls that later fail, which matches YouTube's quota accounting).
"""

from __future__ import annotations

from factory.config.youtube_config import YOUTUBE_QUOTA_COSTS
from factory.errors import QuotaExceededError

QUOTA_COSTS = YOUTUBE_QUOTA_COSTS


class QuotaTracker:
    """Counts documented quota units against a configurable per-run budget."""

    def __init__(self, *, budget_units: int | None = None) -> None:
        self._budget = budget_units
        self._used = 0

    @property
    def budget_units(self) -> int | None:
        return self._budget

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int | None:
        if self._budget is None:
            return None
        return max(self._budget - self._used, 0)

    def cost(self, endpoint: str) -> int:
        """The documented quota cost of one call to ``endpoint``."""
        return QUOTA_COSTS[endpoint]

    def charge(self, endpoint: str, *, provider: str = "youtube") -> int:
        """Charge one call to ``endpoint`` against the budget.

        Raises :class:`QuotaExceededError` when the call would exceed the
        configured per-run budget (quota-aware behavior: stop before spending
        more than allowed).
        """
        cost = self.cost(endpoint)
        if self._budget is not None and self._used + cost > self._budget:
            raise QuotaExceededError(
                f"Discovery quota budget exceeded: {self._used} + {cost} units "
                f"({endpoint}) would exceed the per-run budget of {self._budget} units",
                provider=provider,
                quota_used=self._used,
                quota_limit=self._budget,
            )
        self._used += cost
        return cost

# ADR-0012: Discovery quota and cost control

**Status:** Accepted (Phase 1)
**Date:** 2026-10-08

## Context

YouTube Data API v3 calls consume quota (documented unit costs: search=100,
videos=1, channels=1; default 10,000 units/day per project). Uncontrolled
discovery would burn quota on repeat lookups, retries, and re-runs. Quota
values must never be invented, and the API does not expose remaining quota.

## Decision

* **Count only documented costs.** `QuotaTracker` charges
  `QUOTA_COSTS[endpoint]` before each call (conservative: failed calls count
  too, matching YouTube's accounting) against a configurable per-run budget
  (`YOUTUBE_QUOTA_BUDGET_UNITS_PER_RUN`, default 1,000 units ≈ 10 searches);
  exceeding it raises `QuotaExceededError` before the API call.
* **Never re-spend on completed work:** discovery responses are cached
  (`discovery_cache` table, TTL, hit counting) — search pages by
  (query, params, page token), video/channel details by id. Re-runs and
  retries of completed lookups make no API calls.
* **Deduplicate** video ids within and across pages; enforce configurable
  result limits (`YOUTUBE_DISCOVERY_RESULT_LIMIT`) and page sizes.
* **Record usage** per API call (latency, success/failure, quota metadata in
  `provider_usage.metadata`) — never the API key. The artifact carries
  `quota_units_used`.
* The documented daily default (10,000) is configuration, labeled
  informational; no quota value is ever invented.

## Consequences

* Positive: bounded, auditable spend; retries/resume are free after the first
  success; the strategy extends the Phase 0 cost-control architecture.
* Negative: the per-run budget is the quota guard (the API exposes no
  remaining-quota signal); daily cross-run tracking is a future control-plane
  feature.

# ADR-0009: Cost control foundation

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

The product must eventually operate at low cost. The Phase 0 brief requires
the *foundation* for caching, deduplication, provider usage tracking, token
accounting, quota tracking, configurable model selection, retry limits, and
request budgets — without speculative AI calls.

## Decision

Build cost control into the provider path from day one
(`docs/cost-control.md`):

1. **Caching/dedup:** deterministic requests (temperature 0) cached by a
   SHA-256 request key (SQLite or in-memory, TTL) — the same deterministic
   result is never billed twice.
2. **Usage tracking:** every provider request (success or failure) recorded
   with provider/model/purpose/job/latency/tokens (NULL + `usage_available`
   when unreported — never estimated). Never the API key.
3. **Budgets:** per-job request and token budgets enforced *before* provider
   calls, computed from the persisted usage table (survives restarts,
   auditable).
4. **Model selection:** configurable default model; the connectivity test
   picks the cheapest listed model from verified reported pricing.
5. **Retry limits:** bounded retries for transient errors only (429/5xx),
   honoring `Retry-After`; non-transient errors are never retried.
6. **Quota observability:** CleanAPIs `X-RateLimit-*` headers captured per
   response; the discovery contract exposes `quota_used()` (Phase 1).

## Consequences

* Positive: spend is measurable per job/purpose/model from the first request;
  deterministic work is free after the first call; budgets stop runaway
  jobs; nothing is estimated silently.
* Negative: a small amount of bookkeeping per request (one insert) — the
  price of auditability.

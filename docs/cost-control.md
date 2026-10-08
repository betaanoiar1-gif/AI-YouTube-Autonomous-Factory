# Cost control

The product must eventually operate at low cost. Phase 0 builds the
**foundation**: caching, deduplication, provider usage tracking, token
accounting, quota tracking, configurable model selection, retry limits, and
request budgets. No speculative AI calls exist — every provider call has a
job, a purpose, and a recorded usage row.

## Mechanisms

### 1. Response cache (deduplication)

Deterministic requests (`temperature=0`, `use_cache=True`) are cached by a
SHA-256 key over the normalized request (provider + model + messages +
sampling parameters).

* Backends: `SQLiteLLMCache` (persistent `llm_cache` table, TTL, hit
  counting) and `InMemoryLLMCache` (LRU, tests/no-DB).
* A cache hit returns the stored response (`cached=true`) and records **no**
  new provider usage — the same deterministic result is never billed twice.
* TTL configurable (`CLEANAPIS_CACHE_TTL_SECONDS`, default 24h).

### 2. Provider usage tracking (token accounting)

Every real provider request (success or failure) is recorded in
`provider_usage`:

* `provider`, `model`, `purpose`, `job_id`, `requested_at`, `latency_ms`
* `prompt_tokens`, `completion_tokens`, `total_tokens` — **as reported by
  the provider**; when the provider does not report usage, the fields are
  NULL and `usage_available=false`. Tokens are **never silently estimated**.
* `success`, `error_type`, `request_id`
* **Never the API key.**

This table is the source of truth for spend: per job, per purpose, per
model, over time.

### 3. Request budgets

`BudgetEnforcer` checks a per-job `RequestBudget` **before** any provider
call:

* `max_requests` — number of provider requests for the job;
* `max_total_tokens` — summed reported tokens for the job.

Spend is computed from the persisted `provider_usage` table
(`SQLAlchemyBudgetStore`), so budgets survive restarts and remain auditable.
Exceeding a budget raises `BudgetExceededError` before the provider is
called. Configurable: `CLEANAPIS_BUDGET_MAX_REQUESTS_PER_JOB` (default 1000),
`CLEANAPIS_BUDGET_MAX_TOTAL_TOKENS_PER_JOB` (default 5,000,000).
An `InMemoryBudgetStore` exists for no-database operation (documented
limitation: not shared across processes).

### 4. Configurable model selection

* `CLEANAPIS_MODEL` sets the default model; requests may override per call.
* The connectivity test selects the **cheapest listed model** by verified
  reported pricing (`pricing.input_per_1k + output_per_1k`) when no model is
  configured — deterministic, data-driven, no guessing.
* Model capabilities (context window, `json_mode`, tools, vision) are
  available from `GET /models` for capability-aware routing later.

### 5. Retry limits

`CLEANAPIS_MAX_RETRIES` (default 3) bounds retries for transient errors
(429/5xx). Non-transient errors (401/402/403/404/422) are never retried —
retrying them would only spend budget. Backoff: exponential + jitter,
honoring `Retry-After`.

### 6. Quota tracking (observability)

* CleanAPIs rate-limit state (`X-RateLimit-Limit`, `-Remaining`, `-Reset`,
  and token variants) is captured on every response into
  `LLMResponse.metadata.rate_limits`.
* The discovery provider contract (`DiscoveryProvider.quota_used()`) makes
  YouTube API quota consumption observable (Phase 1).

### 7. Deterministic, minimal AI usage

* The connectivity test performs exactly one minimal completion (tiny
  prompt, `max_tokens=2048` — the documented minimum, temperature 0).
* Structured output uses one correction retry at most.
* Expensive models are never used for simple deterministic operations
  (deterministic operations use the cache; the connectivity probe picks the
  cheapest model).

## What is NOT in Phase 0 (by design)

* Per-project cost budgets and spend dashboards (the data is all there —
  `provider_usage` — the policy layer comes with the control plane UI).
* Automatic model downgrade on budget pressure (budget *enforcement* exists;
  routing policy is a later phase).
* Streaming responses (not needed for pipeline workloads; avoids holding
  connections open).

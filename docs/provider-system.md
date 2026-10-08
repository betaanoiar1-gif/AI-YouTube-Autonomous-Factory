# Provider system

The application never talks to a vendor directly from business logic. It
talks to **provider interfaces** (`factory/providers/base.py`); CleanAPIs is
the real implementation of `LLMProvider` in Phase 0. The purpose is future
provider replacement without rewriting business logic.

## Interfaces

| Interface | Purpose | Phase 0 state |
| --- | --- | --- |
| `LLMProvider` | chat completions + structured output | **implemented: `CleanAPIsProvider`** |
| `ASRProvider` | speech-to-text | contract only |
| `VisionProvider` | image understanding (QA plane) | contract only |
| `EmbeddingProvider` | embeddings (topic clustering) | contract only |
| `ImageProvider` | image generation | contract only |
| `VideoProvider` | video rendering | contract only |
| `TTSProvider` | text-to-speech | contract only |
| `DiscoveryProvider` | YouTube discovery/metrics with quota visibility | **implemented: `YouTubeDiscoveryProvider`** (YouTube Data API v3) |

Unimplemented providers are **contracts, not fakes**: calling them raises
`NotImplementedError` with a pointer to the phase that implements them. No
pretend implementations exist.

`DiscoveryProvider` is the boundary for YouTube data access; the Phase 1
implementation (`YouTubeDiscoveryProvider`) uses the documented YouTube
Data API v3 only (`search`/`videos`/`channels`, API-key auth), respects
quotas (documented unit costs + a per-run budget enforced before calls),
and exposes observable quota consumption (`quota_used()` + per-call usage
records with quota metadata). Scraping, undocumented endpoints, proxy tricks,
and quota evasion are explicitly out of scope for this project. See
`docs/intelligence-plane.md` and `docs/decisions/ADR-0010`.

## Normalized AI request model

Business logic uses only these shapes (`factory/providers/llm/types.py`):

```python
LLMRequest(
    model: str | None,               # None → provider default
    messages: list[LLMMessage],      # role: system|user|assistant|tool
    system: str | None,              # convenience preamble
    temperature: float | None,       # 0.0–2.0
    max_tokens: int | None,
    top_p: float | None,
    stop: list[str] | None,
    seed: int | None,
    json_mode: bool,                 # request JSON-only output
    use_cache: bool,                 # deterministic-request dedup
    cache_ttl_seconds: int | None,
    metadata: dict,                  # caller context (never sent to provider)
)

LLMResponse(
    content: str,
    provider: str,                   # "cleanapis"
    model: str,
    usage: LLMUsage(                 # prompt/completion/total tokens or None
        prompt_tokens: int | None,   #   — never silently estimated
        completion_tokens: int | None,
        total_tokens: int | None,
    ),
    finish_reason: FinishReason,     # stop|length|tool_calls|unknown
    latency_ms: int,
    request_id: str | None,
    cached: bool,
    metadata: dict,                  # e.g. rate-limit header state
)
```

The internal system never depends on CleanAPIs-specific request/response
formats; the provider translates.

## Structured output

`LLMProvider.complete_structured(request, schema)` (default implementation in
the base class, inherited by all LLM providers):

1. requests JSON (`json_mode=True`);
2. parses strictly (one pair of markdown fences tolerated);
3. validates against the pydantic schema (`extra="forbid"` contracts);
4. on failure retries once with a constrained correction request containing
   the parse/validation error;
5. raises `StructuredOutputError` if still invalid — malformed AI output is
   never silently accepted.

## CleanAPIsProvider responsibilities

`factory/providers/cleanapis/provider.py`:

* translates `LLMRequest` ↔ the verified CleanAPIs wire format;
* **tracks usage for every request** (success or failure) — the API key is
  never recorded;
* **deduplicates** deterministic requests (temperature 0) via the response
  cache (SQLite or in-memory);
* **enforces per-job budgets** before calling the provider;
* measures latency; records rate-limit header state in response metadata;
* binds `provider`/`model`/`job_id`/`purpose` context to structured logs.

## CleanAPIsClient

`factory/providers/cleanapis/client.py` is a thin, explicit httpx client (see
ADR-0003 for why no SDK):

* `GET /models` → `list[ModelInfo]` (id, context_window, capabilities,
  pricing) — used by the connectivity test and for capability checks;
* `POST /chat/completions` → `ChatCompletionResult` (raw normalized parse);
* typed error mapping from the documented error envelope (401/402/403/404/
  422/429/502/timeout/connect/malformed);
* retry with exponential backoff + jitter for 429/5xx, honoring
  `Retry-After`;
* captures `X-RateLimit-*` headers into `RateLimitState` (quota
  observability);
* applies the Bearer auth header to injected clients too (a test transport
  can never send an unauthenticated request).

## Provider usage tracking

Every real provider request is recorded (`factory/observability/usage.py`):

* provider, model, purpose, job id, timestamp, latency
* token usage when reported; `usage_available=false` + NULLs when not —
  never estimated
* success/failure, error type, provider request id
* never the API key

Sinks: `SQLAlchemyUsageTracker` (persists to `provider_usage`),
`LoggingUsageTracker` (structured log line), `CompositeUsageTracker`
(fan-out; a failing sink never breaks a request).

## Wiring

`CleanAPIsProvider` takes its dependencies by injection (settings, client,
usage tracker, cache, budget enforcer), which keeps it fully testable with a
mocked HTTP transport. `CleanAPIsProvider.with_sqlite_cache(...)` builds a
provider wired to the database for cache + usage tracking.

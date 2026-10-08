# CleanAPIs integration

CleanAPIs (<https://cleanapis.com>) is the **real LLM provider** for this
project from Phase 0. This document describes only what was **actually
verified** — from the official CleanAPIs documentation — plus the honest
status of the live connectivity test in this environment.

## Verified facts (from official CleanAPIs documentation)

Sources: `https://cleanapis.com/docs/getting-started`,
`/docs/authentication`, `/docs/models`, `/docs/chat-completions`,
`/docs/tool-calling`, `https://cleanapis.com/terms`.

| Fact | Verified value |
| --- | --- |
| Service model | API gateway; single OpenAI-compatible endpoint routing to third-party model providers; meters usage and bills |
| **Base URL** | `https://cleanapis.com/v1` (a `www.` variant is also documented) |
| **Authentication** | `Authorization: Bearer cc_...`; the Anthropic-style `x-api-key` header is also accepted |
| Key format | Keys start with `cc_`; stored server-side only as SHA-256 hashes |
| **Endpoints** | `GET /models`, `GET /models/{id}`, `POST /chat/completions`, `POST /embeddings` |
| Key scopes | `inference` (chat completions), `models:read` (models list), `embeddings`; new keys get all three |
| `/models` response | `{"object": "list", "data": [{id, object, created, owned_by, name, description, type, context_window, capabilities[], pricing: {input_per_1k, output_per_1k}}]}` |
| Model capabilities | `reasoning`, `vision`, `tools`, `streaming`, `json_mode`, `web_search` |
| Pricing | Per-model USD per 1K tokens (`pricing.input_per_1k`, `pricing.output_per_1k`) — enables deterministic cheapest-model selection |
| Chat request | `model`, `messages`, `stream`, `max_tokens` (default 8192; values below 2048 are raised), `temperature`, `top_p`, `tools`, `tool_choice`, `response_format: {"type": "json_object"}`, `stop`, `frequency_penalty`, `presence_penalty`, `seed` |
| Chat response | `{"id", "object", "created", "model", "choices": [{"index", "message": {"role", "content"}, "finish_reason"}], "usage": {"prompt_tokens", "completion_tokens", "total_tokens"}}` |
| finish_reason | `stop`, `length`, `tool_calls` |
| **Error envelope** | `{"error": {"message", "type", "code", "param"?}}` (OpenAI-shaped) |
| Error statuses | `401` authentication_error (invalid_api_key), `402` insufficient_funds, `403` invalid_request_error/insufficient_scope, `404` invalid_request_error/model_not_found, `422` invalid_request_error (validation), `429` rate_limit_error (rate_limit_exceeded), `502` provider_error |
| Rate limits | RPM and TPM per plan; `429` carries a `Retry-After` header; state in `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `X-RateLimit-Limit-Tokens`, `X-RateLimit-Remaining-Tokens` response headers |
| Billing | Plan allowance first, then prepaid USD balance; `402` is returned *before* reaching a model when both are exhausted; tokens count input + output |
| Streaming | Up to one hour with `: ping` keep-alives every 15s |
| Terms | Prompts/outputs are not used to train models |

## Configuration

All CleanAPIs configuration is provider configuration (separate from
application configuration). See `.env.example` for the full list.

| Variable | Default | Meaning |
| --- | --- | --- |
| `cleanapis_API_KEY` | *(none — required)* | Runtime secret. Read from the environment only (matched case-insensitively, so `CLEANAPIS_API_KEY` also works). Never hard-coded, logged, committed, or printed. |
| `CLEANAPIS_BASE_URL` | `https://cleanapis.com/v1` | Verified default. Must be an `http(s)` URL ending in `/v1` (the API root, not a sub-path). |
| `CLEANAPIS_MODEL` | *(empty)* | Default model for LLM calls. Empty = requests must name a model explicitly; the connectivity test then selects the cheapest listed model. |
| `CLEANAPIS_TIMEOUT_SECONDS` | `120` | Per-request timeout (10s connect). |
| `CLEANAPIS_MAX_RETRIES` | `3` | Retries for `429` and `5xx` only. |
| `CLEANAPIS_CACHE_ENABLED` | `true` | Response cache for deterministic requests. |
| `CLEANAPIS_CACHE_TTL_SECONDS` | `86400` | Cache entry TTL. |
| `CLEANAPIS_BUDGET_MAX_REQUESTS_PER_JOB` | `1000` | Per-job request budget. |
| `CLEANAPIS_BUDGET_MAX_TOTAL_TOKENS_PER_JOB` | `5000000` | Per-job token budget. |

`CleanAPISSettings.require_api_key()` raises a safe `ConfigurationError` when
the key is missing; the message never contains key material.

## Authentication

The provider sends `Authorization: Bearer <key>` on every request. The key is
loaded from the environment at client construction; if it is absent the
client refuses to construct (`ProviderConfigurationError`). The key is
registered with the log-redaction layer at startup, so even an accidental log
of the key value is masked.

## Request format (as sent by `CleanAPIsClient`)

`POST {base_url}/chat/completions`:

```json
{
  "model": "<model id>",
  "messages": [{"role": "system|user|assistant", "content": "..."}],
  "temperature": 0.0,
  "max_tokens": 2048,
  "top_p": 0.9,
  "stop": ["END"],
  "seed": 42,
  "response_format": {"type": "json_object"}
}
```

Only fields set on the normalized `LLMRequest` are sent. `response_format` is
sent only when `json_mode` is requested (the model's `json_mode` capability is
listed in `GET /models`; the provider also safe-parses regardless — see
"Structured output").

## Response normalization

The wire response is normalized into `LLMResponse`:

```python
LLMResponse(
    content: str,                 # choices[0].message.content (must be a string)
    provider: "cleanapis",
    model: str,                   # echoed model id from the response
    usage: LLMUsage(              # prompt/completion/total tokens, or None
        prompt_tokens: int | None,    # when the provider did not report them
        completion_tokens: int | None,
        total_tokens: int | None,
    ),
    finish_reason: FinishReason,  # stop | length | tool_calls | unknown
    latency_ms: int,
    request_id: str | None,       # provider request id (for support/debug)
    cached: bool,
    metadata: {"rate_limits": {...}},  # X-RateLimit-* header state
)
```

Malformed responses (invalid JSON, missing/empty `choices`, non-string
`content`) raise `MalformedResponseError` — they are never silently accepted.

## Error handling

All HTTP errors are mapped from the documented envelope to typed, sanitized
errors (`factory/errors.py`). Error messages come from the provider envelope;
they never contain the API key (the key is only ever sent in a header).

| HTTP | Exception | Retried? |
| --- | --- | --- |
| 401 | `AuthenticationError` | no |
| 402 | `InsufficientFundsError` | no |
| 403 | `ScopeError` | no |
| 404 | `ModelNotFoundError` | no |
| 422 | `ProviderValidationError` | no |
| 429 | `RateLimitError` (carries `retry_after_seconds`) | yes — honors `Retry-After` |
| 5xx | `ProviderHTTPError` | yes — exponential backoff + jitter |
| timeout | `ProviderTimeoutError` | no |
| connect failure | `ProviderUnavailableError` | no |
| other HTTP | `ProviderHTTPError` | no |
| malformed body | `MalformedResponseError` | no |

Retries: at most `CLEANAPIS_MAX_RETRIES` additional attempts, exponential
backoff (`0.5 * 2^attempt` + jitter), and the server-provided `Retry-After`
delay for 429s.

## Structured output

`LLMProvider.complete_structured(request, schema)` (default implementation in
`factory/providers/base.py`, inherited by `CleanAPIsProvider`):

1. requests JSON (`json_mode=True` → `response_format: {"type":
   "json_object"}` when the model advertises `json_mode`);
2. parses the response **strictly** (a single pair of markdown fences is
   tolerated, nothing else);
3. validates against the pydantic schema (`extra="forbid"` contracts);
4. on failure, retries **once** with a constrained correction request that
   includes the parse/validation error;
5. raises `StructuredOutputError` if the output is still invalid.

Malformed AI output is never silently accepted.

## Usage tracking

Every request (success **or** failure) is recorded via `UsageTracker` into the
`provider_usage` table (or a structured log line when no database is bound):

* provider, model, purpose, job id
* request timestamp, latency
* `prompt_tokens`, `completion_tokens`, `total_tokens` — **NULL when the
  provider did not report them**, with `usage_available=false`; tokens are
  never silently estimated
* success flag, error type, provider request id

The API key is **never** recorded. Budget enforcement computes per-job spend
from this table, so budgets survive restarts and remain auditable.

## Caching / deduplication

Deterministic requests (`temperature=0`, `use_cache=True`) are cached by a
SHA-256 key over the normalized request (provider + model + messages +
sampling parameters). Backends: SQLite (`llm_cache` table, persistent) or
in-memory LRU. A cache hit returns the stored response marked `cached=true`
and records no new provider usage.

## Connectivity test

**Purpose:** a REAL but minimal verification that (1) the API key is
available, (2) authentication works, (3) the configured endpoint is
reachable, (4) a valid model is available, (5) a minimal request succeeds,
(6) the response can be parsed, and (7) errors are handled safely.

**Implementation:** `factory/providers/cleanapis/connectivity.py`
(`run_connectivity_test`), exposed as:

```bash
python -m factory.cli cleanapis test-connection     # CLI, exit code 0/1
pytest -m live tests/integration -v                  # same check as a test
```

**What it does:**

1. Checks `cleanapis_API_KEY` is present (value never displayed).
2. `GET /models` with the key — verifies authentication + endpoint
   reachability in one call; parses the model list.
3. Selects a model: the configured `CLEANAPIS_MODEL` if set (must exist in
   the listing), otherwise the **cheapest listed model** by reported
   `pricing.input_per_1k + pricing.output_per_1k` (ties broken by model id —
   deterministic). This satisfies "use the cheapest/safest available model"
   using only verified pricing data.
4. Sends exactly ONE minimal completion: user message
   `"Reply with exactly the word: OK"`, `temperature=0`, `max_tokens=2048`
   (the documented minimum). No content generation of any substance; the
   response content is not stored.
5. Parses the response (content, finish_reason, usage).
6. Every failure mode produces a typed, sanitized step result in a
   `ConnectivityReport` (no secrets, no key leakage, no stack traces with
   credentials). The report is printed as JSON; exit code 0 on pass, 1 on
   fail.

### Simulated automated test (offline): PASS

The connectivity-test **code path** (`run_connectivity_test` + the full
production `CleanAPIsProvider`/`CleanAPIsClient` stack) is exercised
automatically, without any credentials, against a realistic local HTTP
simulation of the CleanAPIs API — see `docs/testing-strategy.md` ("The
simulated CleanAPIs layer") and `tests/simulated/`. Coverage includes the
connectivity test passing end-to-end against the simulation (all 6 steps,
cheapest-model selection from simulated pricing) and reporting an
authentication failure safely. **41 simulated/offline tests pass**
(`make test-simulated`). These tests are labeled `simulated`/`offline` and
never contact the real API.

### REAL CleanAPIs connectivity: NOT RUN — owner test pending

The live test against the real `https://cleanapis.com` API **has not been
run** and must be performed **manually by the project owner**. It cannot
execute in the build sandbox, for two independent environment reasons (both
verified directly):

1. **`cleanapis_API_KEY` is not set** in the sandbox environment. The test
   correctly fails step 1 with `ConfigurationError` (verified:
   `python -m factory.cli cleanapis test-connection` → `overall_pass:
   false`, exit 1, no secrets in output).
2. **Outbound network access to `cleanapis.com` is blocked** by the sandbox
   egress allowlist (verified: TLS connect to `cleanapis.com:443` and
   `www.cleanapis.com:443` fails with `SSL_ERROR_SYSCALL`; only
   github.com, pypi.org, registry.npmjs.org and similar hosts are
   reachable). Even with a key present, the test could not reach the API
   from this sandbox.

**Owner-run remediation:** in an environment where `cleanapis_API_KEY` is
set and egress to `https://cleanapis.com` is allowed:

```bash
export cleanapis_API_KEY=cc_...
python -m factory.cli cleanapis test-connection
# or
pytest -m live tests/integration -v
```

The live test remains available, clearly separated (marked `live`, skipped
without a key), and no real connectivity is claimed anywhere in this
repository.

No endpoint, model, authentication format, or response field was invented:
everything above comes from the official documentation, and the integration
is built so the live test verifies the assumptions end-to-end before Phase 1
relies on them.

# ADR-0003: Thin HTTP client for CleanAPIs (no SDK)

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

CleanAPIs is OpenAI-compatible, so the official `openai` SDK pointed at
`base_url="https://cleanapis.com/v1"` would work (it is the documented
quickstart). The Phase 0 brief requires the endpoint, auth, models, and
compatibility to be **verified**, and requires explicit usage tracking,
error mapping, retries, and quota observability.

## Decision

Implement `CleanAPIsClient` as a thin, explicit **httpx** client
(`factory/providers/cleanapis/client.py`) instead of using an SDK.

Rationale:

* The provider boundary owns the translation between normalized models and the
  wire format — explicit, reviewable, and free of SDK version drift.
* Full control over what is measured: latency, usage fields, `X-RateLimit-*`
  headers, request ids, and the exact error envelope mapping.
* Fully testable without network: `httpx.MockTransport` exercises success,
  401/402/403/404/422/429/5xx, timeout, connect failure, and malformed
  responses deterministically.
* No extra dependency surface; the verified wire format is small (models +
  chat completions).
* Auth is applied to injected clients too, so a test transport can never
  send an unauthenticated request.

## Consequences

* Positive: explicit, tested, observable; no SDK behavior to debug; the
  connectivity test verifies the real wire format end-to-end.
* Negative: we maintain the (small) request/response translation ourselves —
  acceptable because the surface is two endpoints and the formats are
  verified and stable per the provider's OpenAI-compatibility commitment.
* If the API grows (streaming, embeddings), the same client extends; the
  normalized model absorbs the change.

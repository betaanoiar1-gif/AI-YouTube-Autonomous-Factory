# ADR-0002: Provider boundary

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

The product will eventually use many AI/media capabilities (LLM, ASR, vision,
embeddings, image, video, TTS) and a YouTube data source. Business logic must
not depend on any vendor's wire formats, so providers can be replaced without
rewriting callers.

## Decision

Define provider **interfaces** in `factory/providers/base.py`:

`LLMProvider`, `ASRProvider`, `VisionProvider`, `EmbeddingProvider`,
`ImageProvider`, `VideoProvider`, `TTSProvider`, `DiscoveryProvider`.

* CleanAPIs is the **real** implementation of `LLMProvider` in Phase 0
  (`factory/providers/cleanapis/`).
* All other providers are **contracts only** — calling them raises
  `NotImplementedError` with a pointer to the implementing phase. No fake
  implementations pretend to work.
* Business logic depends on the normalized `LLMRequest`/`LLMResponse` model
  (`factory/providers/llm/types.py`), never on CleanAPIs-specific shapes.
* `complete_structured` (safe JSON parse + schema validation + one correction
  retry) is implemented once in the `LLMProvider` base class so every future
  LLM provider inherits it.
* `DiscoveryProvider` is the boundary for YouTube data access: documented
  API only, quota/rate-limit respect, observable quota consumption.

## Consequences

* Positive: swapping providers (or adding a fallback) touches one
  implementation module; structured-output safety is uniform; the YouTube
  boundary is explicit and auditable.
* Negative: a thin abstraction layer to maintain (justified: it is the
  project's stated architectural principle).

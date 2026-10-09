# ADR-0013: Research-plane provider boundary

**Status:** Accepted (Phase 2)
**Date:** 2026-10-08

## Context

Phase 2 (research plane) must discover sources, collect them safely, extract
evidence, and verify claims — without coupling the research engine to any
specific search engine, website, LLM, or scraping implementation. Future
providers must be addable without rewriting the engine.

## Decision

Introduce four provider-neutral contracts in `factory/providers/research/`:

* `ResearchProvider` — opportunity → research plan;
* `SourceProvider` — source discovery AND safe source collection;
* `EvidenceExtractor` — collected documents → structured evidence;
* `ClaimVerifier` — deterministic cross-source verification.

Normalized models live in `factory/providers/research/types.py`; the engine
(`factory/research/`) composes the four contracts. Phase 2 implementations are
deterministic (planner, extractor, verifier) plus `WebSourceProvider`
(documented free search API + SSRF-protected collection). An LLM-backed
extractor can later implement `EvidenceExtractor` — optionally through the
existing `LLMProvider` abstraction — without touching the engine.

## Consequences

* Positive: the research engine is vendor-neutral and testable; providers are
  swappable; the offline simulation exercises the production code path.
* Negative: a small abstraction layer to maintain (justified: it is the
  stated architectural principle, and the simulation proves the boundary).

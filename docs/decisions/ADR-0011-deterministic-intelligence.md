# ADR-0011: Deterministic, provider-independent intelligence

**Status:** Accepted (Phase 1)
**Date:** 2026-10-08

## Context

Market analysis, topic/pattern clustering, and opportunity detection must be
measurable, explainable, reproducible, and cheap. There is pressure to "make
clustering intelligent" with an LLM — but an LLM dependency would make
scoring non-reproducible, unexplainable, and costly, and the Phase 1 brief
explicitly forbids adding an LLM just to make clustering appear intelligent.

## Decision

The intelligence plane is **deterministic and provider-independent**:

* analysis computes documented, measurable signals (views, age-adjusted
  performance, velocity, engagement, channel-relative performance, recency)
  with min-max normalization and published weights/formulas;
* clustering (`DeterministicClusterer`) finds recurring topics (n-grams),
  clusters (connected components), questions, formats, underserved themes,
  and saturation — all from titles/metrics metadata, no embeddings, no LLM;
* opportunity detection scores demand/gap/novelty/confidence with published
  formulas and deterministic ids (uuid5 of project+topic);
* missing source data yields `None` metrics — values are never invented;
* the `Clusterer` protocol is the explicit seam: a future embedding/LLM
  clusterer can replace the deterministic layer without changing callers.

## Consequences

* Positive: reproducible (same input + timestamp → identical output),
  explainable (every score traces to a formula and evidence), cheap (no LLM
  spend in the intelligence plane), and testable (golden-value tests).
* Negative: less semantic depth than learned clustering — accepted for Phase 1;
  the protocol makes the upgrade path explicit.

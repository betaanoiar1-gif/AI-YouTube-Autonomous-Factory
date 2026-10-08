# ADR-0015: Deterministic verification — never "number of sources = truth"

**Status:** Accepted (Phase 2)
**Date:** 2026-10-08

## Context

Cross-source verification must be deterministic, explainable, and honest. The
naive rule "more sources = more truth" is wrong: syndicated copies of one
article are not independent confirmations, and absence of evidence is not
confirmation either.

## Decision

`DeterministicClaimVerifier` implements verification foundations that are
deterministic and auditable:

* **source independence** by distinct registered domain, with syndicated
  duplicates (identical content fingerprints) counted once;
* **evidence coverage** (supporting vs contradicting ratio);
* **authority weighting** from justified classification signals only;
* a **published confidence formula**:
  `coverage x (0.5 + 0.5 x independence) x (0.5 + 0.5 x authority)`;
* statuses `UNVERIFIED / SUPPORTED / MULTI_SOURCE_SUPPORTED / CONTESTED /
  CONTRADICTED / INSUFFICIENT_EVIDENCE` — absence of evidence is never
  confirmation;
* **contradiction handling** preserves both sides with provenance, marks
  claims `CONTESTED`/`CONTRADICTED`, records the conflict unresolved, and never
  silently chooses a side or invents a resolution.

## Consequences

* Positive: every verification outcome is explainable and reproducible; the
  failure modes (syndication, disagreement, insufficient evidence) are first
  class and tested.
* Negative: less semantic depth than learned verification — the `ClaimVerifier`
  contract is the explicit seam for a future learned/LLM-backed verifier.

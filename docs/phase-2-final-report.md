# Phase 2 — Final Report (Research Plane)

**Project:** AI YouTube Autonomous Factory
**Phase:** 2 — Research Plane (COMPLETE; Phase 3 not started)
**Date:** 2026-10-08
**Branch:** `arena/a87c5fb1-ai-youtube-autonomous-factory` (PR to `main`)

---

## STATUS

| Item | Status |
| --- | --- |
| **SIMULATED RESEARCH TEST** | ✅ **PASS** — 13/13 simulated/offline research integration tests green (`pytest -m simulated -k research`) |
| **REAL external connectivity (search API / real sources)** | ⏳ **NOT RUN — OWNER TEST PENDING** — reserved for a manual owner-run against the real documented search API and real sources |
| Research pipeline (opportunity → research_report) | ✅ Implemented, tested, integrated with the job system |
| Phase 0 + Phase 1 regression | ✅ Green (full suite: 516 passed, 1 deselected, 0 failed) |

**Overall Phase 2 verdict: COMPLETE.** All Phase 2 deliverables are
implemented and every executable quality gate passes. The only item that
cannot execute in the build sandbox (live connectivity to the real external
search API / real sources) is honestly reported as NOT RUN — no real
connectivity is claimed, and no real credentials were requested, accessed,
created, or required.

**SIMULATED RESEARCH TEST: PASS**
**REAL CONNECTIVITY (per external provider): NOT RUN — OWNER TEST PENDING**
(there is exactly one external provider in Phase 2 — the documented free
search API used for source discovery — plus arbitrary source URLs collected
through the SSRF-protected fetcher; neither was contacted live).

---

## ARCHITECTURE CHANGES

* New `factory/providers/research/` — provider-neutral contracts:
  `ResearchProvider`, `SourceProvider`, `EvidenceExtractor`, `ClaimVerifier`
  + normalized models (`SourceCandidate`, `CollectedSource`, `EvidenceDraft`,
  `ClaimDraft`, `VerificationResult`, `ContradictionFinding`).
* New `factory/research/` — `DeterministicResearchPlanner`,
  `WebSourceProvider` (production source provider: documented free search API
  + safe collection), `DeterministicEvidenceExtractor`,
  `DeterministicClaimVerifier`, source utilities (classification, ids, caches),
  `ResearchEngine` (8 checkpointed stages), `ResearchPipeline` (job handler).
* New `factory/security/fetch.py` — `SafeFetcher` (SSRF-protected retrieval)
  + `factory/security/urls.py` extensions (`canonicalize_url`,
  `url_fingerprint`, `registered_domain`).
* New `factory/config/research_config.py` (`ResearchSettings`); wired into
  settings-cache resets. `allow_loopback` is deliberately NOT a setting.
* CLI: `--job-type research` on `factory pipeline run`; a clean
  `_PipelineFacade` merges intelligence + research handlers.
* Contracts: 4 new artifact types (`research_plan`, `source`, `evidence`,
  `research_claim`) + `research_report` extended backward-compatibly.
* DB: migration `825b2b619896` — `source_cache` table.

## FILES CHANGED

**Created (production):** `factory/config/research_config.py`,
`factory/providers/research/{__init__,types,base,web}.py`,
`factory/research/{__init__,sources,planner,extractor,verifier,engine,pipeline}.py`,
`factory/security/fetch.py`,
`alembic/versions/825b2b619896_source_cache_for_research_plane.py`,
`docs/research-plane.md`, `docs/phase-2-final-report.md`,
`docs/decisions/ADR-0013/0014/0015`.

**Created (tests):** `tests/simulated/research_server.py` (local simulation of
the search API + source documents, distinct loopback "domains"),
`tests/simulated/test_research_simulated.py` (13 tests),
`tests/unit/test_safe_fetch.py`, `test_research_config.py`,
`test_research_sources.py`, `test_research_planner.py`,
`test_research_extractor.py`, `test_research_verifier.py`,
`test_research_engine.py`, `test_research_pipeline.py`,
`test_schemas_phase2.py`.

**Modified (production):** `factory/schemas/artifacts.py` (4 new contracts +
  `research_report` extension), `factory/storage/models.py`
  (`SourceCacheEntry`), `factory/security/urls.py` (canonicalization +
  fingerprints), `factory/config/settings.py` (research settings cache),
  `factory/cli.py` (research job + facade).

**Modified (config/docs):** `.env.example` unchanged (no new secrets — the
research plane needs no credentials), `README.md`, `docs/architecture.md`,
`docs/domain-model.md`, `docs/job-system.md`, `docs/provider-system.md`,
`docs/artifact-system.md`, `docs/testing-strategy.md`, `docs/cost-control.md`,
`schemas/artifacts/{research_plan,source,evidence,research_claim,research_report}.schema.json`.

**Modified (tests):** `tests/unit/test_schemas_artifacts.py` (Phase 2 samples),
`tests/unit/test_migrations.py` (22 tables).

## DATABASE CHANGES

Migration `825b2b619896_source_cache_for_research_plane.py`: new
`source_cache` table (url_fingerprint PK, canonical_url, content_fingerprint,
bounded content, content_type, byte_size, status, error, collected_at,
expires_at, hit_count, metadata). Only this structure was added — sources,
evidence, and claims live in versioned artifacts; research-job metadata is
`pipeline_jobs`. Verified: `upgrade head` → 22 tables; `downgrade base` →
clean; re-upgrade → clean; `alembic check` → **no drift**.

## SCHEMAS (contracts)

New artifact contracts (pydantic + JSON Schema 2020-12, cross-validated):
`research_plan` (plan from an opportunity), `source` (justified type/authority,
fingerprints, collection status — metadata only), `evidence` (bounded passage,
location, confidence, structured subject/predicate/value), `research_claim`
(verification status + provenance + independent-source count). `research_report`
extended backward-compatibly (Phase 0 payloads still validate — tested): new
optional fields for question, findings, verified/contested claims, evidence
map, source list + quality, contradiction details, confidence summary,
limitations, lineage, timestamps; the pipeline populates the legacy fields too.
`schema_version` stays `1.0.0` (non-breaking).

## JOBS

`RESEARCH` job on the existing job system: input = `opportunity_list`
artifact (by id/payload/latest) + selected opportunity id + depth/language/
audience. 8 stages, each checkpointed: PLAN → SOURCE_DISCOVERY →
SOURCE_COLLECTION → EVIDENCE_EXTRACTION → CLAIM_BUILDING → VERIFICATION →
CONTRADICTION_ANALYSIS → REPORT. A failed later stage resumes without
repeating completed stages (tested). Outputs: `research_plan` artifact
(planning stage) + `research_report` artifact (job output). CLI:
`factory pipeline run --job-type research`.

## PROVIDERS

Four provider-neutral contracts (`ResearchProvider`, `SourceProvider`,
`EvidenceExtractor`, `ClaimVerifier`) with normalized models — future
providers (other search backends, LLM-backed extraction via the existing
`LLMProvider` abstraction, learned verification) slot in without rewriting
the engine. Phase 2 implementations: `DeterministicResearchPlanner`,
`WebSourceProvider` (documented free MediaWiki search API + `SafeFetcher`),
`DeterministicEvidenceExtractor`, `DeterministicClaimVerifier`.

## SOURCE DISCOVERY

`WebSourceProvider.discover_sources` uses the documented **Wikipedia
MediaWiki API** (`action=query&list=search`) — free, no key, no scraping.
Candidates carry: URL, canonical URL + fingerprint, title, publisher, date,
justified source type + authority score + indicators, discovery query,
relevance score. Classification is justified (search-API hint, then .gov/.mil/
.edu domain signals, then reference domains, else `unknown` with low authority
— high rank is never authority). Pagination via `srlimit`; retries on 429/5xx.

## SOURCE COLLECTION

`SafeFetcher` (SSRF protection): HTTPS by default (loopback HTTP only via a
constructor flag for the offline simulation — never env-configurable);
original-URL validation (embedded credentials rejected before canonicalization
drops them); resolved-IP blocking (private/loopback/link-local/reserved/
multicast/unspecified/CGNAT); manual redirect validation with re-checked
targets; timeout, streamed size cap, content-type allowlist; no shell, no
filesystem; sanitized errors; unretrievable sources keep metadata and are
marked `failed` (content never invented).

## EVIDENCE

Deterministic extraction: HTML→text (stdlib, skipping title/script/style),
sentence splitting, date/number/text fact patterns with structured
subject/predicate/value (value-free predicates so conflicting values group
together), bounded passages, locations, confidence, per-source caps.
Provenance preserved: evidence → source id → source metadata.

## CLAIMS

Claims are built by grouping evidence on (subject, predicate) with the modal
value. Each claim: id, statement, type, importance, evidence refs, source
count, supporting/contradicting source ids, confidence, verification status,
independent-source count, notes. Statuses: `UNVERIFIED`, `SUPPORTED`,
`MULTI_SOURCE_SUPPORTED`, `CONTESTED`, `CONTRADICTED`,
`INSUFFICIENT_EVIDENCE`. Absence of evidence is never confirmation.

## VERIFICATION

Deterministic cross-source verification: source independence by distinct
registered domain with syndicated duplicates (identical content fingerprints)
counted once — **never "number of sources = truth"**; evidence coverage;
authority weighting from justified signals; published confidence formula
`coverage x (0.5 + 0.5 x independence) x (0.5 + 0.5 x authority)`.

## CONTRADICTIONS

Conflicting numbers/dates (value comparison), explicit disagreement (passage
markers), and incompatible claims are detected per (subject, predicate) group.
Both sides and their provenance are preserved; claims are marked
`CONTESTED`/`CONTRADICTED`; conflicts are recorded `unresolved`. The system
never silently chooses a side and never invents a resolution.

## TESTS / RESULTS

**516 passed, 1 deselected (live), 0 failed** — `pytest`. Coverage per the
Phase 2 brief:

* **Research plan:** opportunity → plan, subquestions, required facts,
  disputed questions, determinism, no competitor copying.
* **Source discovery:** normalization, classification (justified, never
  assumed), dedup, source-type variety, invalid response, provider failure.
* **Source collection:** valid HTTPS source, timeout, oversized, invalid
  content type, redirect (safe + unsafe), unsafe URL, localhost/private IP
  (SSRF), malformed response, unavailable source, no shell/filesystem.
* **Evidence:** extraction, provenance, location, confidence, bounds, caps.
* **Claims:** supported, multi-source, unsupported, contested, evidence refs.
* **Verification:** independent sources, syndicated duplicates (not
  independent), conflicting evidence, authority weighting, insufficient
  evidence.
* **Contradictions:** numbers, dates, explicit disagreement, unresolved
  conflicts, both sides preserved.
* **Pipeline:** complete research pipeline, checkpoint resume, failed-stage
  retry (earlier stages never repeat), idempotency, artifact integrity,
  lineage, no duplicate source collection.
* **Security:** SSRF prevention (every blocked range), URL validation,
  credential rejection, secret redaction, safe errors, no shell execution.
* **Simulated research (offline):** 13 tests — the production pipeline against
  `tests/simulated/research_server.py`: multiple source types, supporting +
  contradictory evidence, syndication, insufficient evidence, malformed
  sources, timeout, oversized/wrong-type, unsafe redirect (SSRF), checkpoint
  resume, no duplicate collection, idempotency, artifact integrity, originality
  boundary, deterministic final report.
* **Phase 0 + Phase 1 regression:** all green (382 tests) + 41 CleanAPIs and
  19 YouTube simulated tests.

## LINT

`ruff check .` — **All checks passed**. `ruff format --check .` — **139 files
formatted, clean**.

## TYPECHECK

`mypy factory tests` — **Success: no issues found in 111 source files**.

## MIGRATIONS

`alembic upgrade head` → 22 tables · `downgrade base` → clean · re-upgrade →
clean · `alembic check` → **No new upgrade operations detected** (no drift).

## SECURITY REVIEW

* **Secrets:** the research plane requires NO credentials (documented free
  search API). Repo-wide secret scan: CLEAN (only clearly-fake offline test
  values; no `.env` exists). Secret-safe logging verified end-to-end.
* **SSRF:** every blocked range tested (private, loopback, link-local,
  reserved, multicast, unspecified, CGNAT, cloud metadata); redirects to
  private addresses blocked; loopback allowed only via the simulation-only
  constructor flag (never configurable); original-URL credential validation
  before canonicalization (a real bug found and fixed during this phase).
* **URLs:** HTTPS by default; embedded credentials rejected; canonicalization
  for dedup.
* **Subprocesses:** no shell execution anywhere in the research plane (the
  fetcher is HTTP-only); the Phase 0 safe-runner remains tested.
* **Path traversal:** artifact store containment unchanged + tamper tests.
* **Dependency changes:** none (stdlib + existing deps only).
* **Unsafe API inputs:** typed config validation; strict provider response
  validation (malformed → `MalformedResponseError`); size caps.
* **Sensitive data handling:** artifacts store metadata + bounded passages
  only — never full source copies; competitor content never copied
  (originality-boundary tests).

## ISSUES FOUND / FIXED

1. **Security bug (high impact):** `check_url_safety` canonicalized before
   validating, and canonicalization drops userinfo — embedded credentials
   were never rejected. Fixed by validating the original URL first; test
   added.
2. `is_blocked_ip` missed the RFC 6598 CGNAT range (100.64.0.0/10) on
   Python 3.11 — added explicitly; test added.
3. Extractor: HTML `<title>` polluted subject extraction — the text converter
   now skips title/script/style; predicates made value-free so conflicting
   values group for contradiction detection; leading articles stripped for
   stable subjects.
4. Engine: collected-source content now flows through the source cache
   (checkpoints never carry large content); engine-level cache put makes
   caching provider-agnostic.
5. `source_to_item_payload` produced non-JSON-safe datetimes (broke job
   checkpoints) — now validated + serialized through the contract model.
6. Contradictions were dropped between verification and the report (internal
   key vs `extra="forbid"` contract) — plumbed correctly with stripping
   before validation.
7. Verifier test helpers used hardcoded source ids — rewritten with real
   fingerprint-based ids.
8. Planner determinism (timestamp) — injectable `now`; test uses a fixed
   moment.
9. CLI handler merge was a monkey-patch — replaced with a typed
   `_PipelineFacade`.
10. Test-design fixes: behavior-queue ordering (search calls run before page
    fetches), retry counts, 404 handler, CGNAT/loopback expectations, RUF/SIM
    lint issues across the new files.

All issues fixed and the full suite re-run green.

## LIMITATIONS

1. **REAL external connectivity: NOT RUN — owner test pending.** Source
   discovery is verified against a realistic local simulation of the
   documented search API, and collection against simulated sources; the live
   owner-run against the real search API and real sources has not been
   performed (no credentials needed or requested; the sandbox blocks egress).
   Owner-run: `python -m factory.cli pipeline run --job-type research …`.
2. Extraction and verification are deterministic and auditable but not
   semantically deep — the `EvidenceExtractor`/`ClaimVerifier` contracts are
   the seams for LLM-backed/learned implementations.
3. DNS-rebinding TOCTOU window in the SSRF check (documented; full pinning
   would need a custom transport).
4. Source breadth depends on the configured search API (Wikipedia by
   default).

## SIMULATED CONNECTIVITY STATUS

**SIMULATED RESEARCH TEST: PASS** — 13/13 simulated/offline integration tests
exercise the production research pipeline against realistic local simulations
(a MediaWiki-shaped search API and deterministic source documents with
agreeing sources, a conflicting date, an exact syndicated duplicate, and an
explicit-disagreement passage; distinct loopback addresses act as distinct
source domains), with no credentials and no real network.

## REAL CONNECTIVITY STATUS

**REAL CONNECTIVITY: NOT RUN — OWNER TEST PENDING** — for every real external
provider (the documented free search API and arbitrary real source URLs).
No real credentials were requested, accessed, created, or exposed, and no real
connectivity is claimed anywhere in this repository.

## FINAL VERDICT

**Phase 2 COMPLETE.** The research plane is implemented end to end
(opportunity → research plan → sources → evidence → verified claims →
contradictions → research report), integrated with the job system, extends
the Phase 0/1 contracts backward-compatibly, enforces the originality
boundary, and passes every quality gate (516/382 automated tests, lint,
format, typecheck, migrations, SSRF/security scans). Phase 0 and Phase 1
regressions remain green. The only unexecuted item — live external
connectivity — is honestly reported as NOT RUN — OWNER TEST PENDING.

## RECOMMENDED NEXT PHASE

1. **Owner:** run the research job against the real documented search API and
   real sources (command above) and record the result.
2. **Phase 3 (Content plane):** opportunities + research reports → content
   briefs, narrative structures, scripts, citations, visual planning.

**STOP — Phase 3 not started.**

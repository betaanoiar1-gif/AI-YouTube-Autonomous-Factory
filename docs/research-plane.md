# Research plane (Phase 2)

The research/evidence pipeline. It consumes the Phase 1 `opportunity_list`
artifact and produces independently researched evidence:

```
Opportunity (opportunity_list artifact)
  → RESEARCH job
      PLAN → SOURCE_DISCOVERY → SOURCE_COLLECTION → EVIDENCE_EXTRACTION
           → CLAIM_BUILDING → VERIFICATION → CONTRADICTION_ANALYSIS → REPORT
  → research_plan artifact + research_report artifact
```

Run it (owner):

```bash
python -m factory.cli pipeline run --job-type research --project-id proj-1 \
  --payload '{"opportunity_list_artifact_id": "<artifact-id>", "opportunity_id": "<id>", "depth": "standard"}'
```

## Originality boundary

This phase is research, **not** competitor-content rewriting. Competitor videos
are market/context evidence only (they motivated the opportunity). The research
plan is generated from the opportunity's topic and audience question; sources
come from the source provider (never YouTube); claims come from collected
sources. Tests assert no competitor title appears anywhere in the research
artifacts. No scripts are generated; no competitor transcripts/narration are
reproduced.

## Provider-neutral architecture

Four contracts (`factory/providers/research/base.py`) keep the engine decoupled
from any search engine, website, LLM, or retrieval implementation:

| Interface | Responsibility | Phase 2 implementation |
| --- | --- | --- |
| `ResearchProvider` | opportunity → research plan | `DeterministicResearchPlanner` |
| `SourceProvider` | source discovery + safe collection | `WebSourceProvider` |
| `EvidenceExtractor` | documents → structured evidence | `DeterministicEvidenceExtractor` |
| `ClaimVerifier` | cross-source verification | `DeterministicClaimVerifier` |

Normalized models live in `factory/providers/research/types.py`
(`SourceCandidate`, `CollectedSource`, `EvidenceDraft`, `ClaimDraft`,
`VerificationResult`, `ContradictionFinding`). Future providers (other search
backends, an LLM-backed extractor via the existing `LLMProvider` abstraction,
learned verification) implement the contracts without rewriting the engine.

## Research plan

Derived from the opportunity (topic, audience question, demand signals).
Identifies: the central question, subquestions (historical, quantitative,
disputed, contextual, comparative), required facts, source requirements, and
verification requirements. Deep depth adds subquestions and primary-source
requirements. Deterministic; never copies competitor titles/descriptions.

## Source model and discovery

A source carries: URL, canonical URL + fingerprint, title, publisher,
publication date, source type, discovery query, relevance score, authority
score + indicators, collection status/error, collected-at, content fingerprint,
byte size, content type.

Source types: `primary`, `government`, `academic`, `journalism`, `reference`,
`secondary`, `unknown`.

**Classification is justified, never assumed:** a search-API type hint takes
precedence; otherwise domain suffixes (.gov/.mil → government, .edu →
academic); well-known reference domains → reference; otherwise `unknown` with a
low authority score. A highly-ranked unknown source is never treated as
authoritative.

Discovery uses the documented **Wikipedia MediaWiki API** (`action=query&list=
search`) — free, no key, no scraping. The search API URL and page-path prefix
are configurable, so the same production code path runs against the offline
simulation.

## Source collection (safe retrieval)

`SafeFetcher` (`factory/security/fetch.py`) — SSRF-protected HTTP retrieval:

* **HTTPS by default**; plain HTTP only for loopback hosts and only with an
  explicit constructor flag that exists solely for the offline simulation
  (never configurable via environment);
* **URL validation** on the original URL (no embedded credentials) and the
  canonical URL;
* **SSRF protection** — the hostname is resolved and every resolved IP is
  checked against blocked ranges: private, loopback, link-local, reserved,
  multicast, unspecified, and the RFC 6598 CGNAT range. With the loopback
  flag, only loopback is permitted; private ranges are always blocked;
* **Redirect validation** — redirects are followed manually, each target
  re-validated (scheme + resolved IP), capped at `max_redirects`;
* **Limits** — timeout, maximum response size (streamed cap), content-type
  allowlist (html/plain/markdown/json/xml);
* **No shell execution, no filesystem access**;
* **Sanitized errors** — reason + credential-free URL only;
* sources that cannot be retrieved keep their metadata and are marked
  `failed` — content is never invented.

Known limitation (documented): the IP check resolves before the request, so a
DNS-rebinding TOCTOU window remains; full pinning would need a custom
transport.

## Evidence extraction

`DeterministicEvidenceExtractor` converts collected text (HTML is reduced to
text via a stdlib parser that skips title/script/style) into structured
evidence: claim, supporting passage (bounded), location, confidence, and
structured subject/predicate/value/value_type fields for contradiction
detection. Date and number facts get high confidence; generic declarative
sentences are lower-confidence text evidence. Questions are not evidence.
Passages are bounded — no large source copies are stored. An LLM-backed
extractor can implement the same contract later (optionally through the
existing `LLMProvider` abstraction).

## Claims model

`VerifiedClaim` (the `research_claim` contract): claim id, statement, type
(fact/estimate/opinion/disputed), importance, evidence references, source
count, supporting/contradicting source ids, confidence, verification status,
independent source count, notes.

Verification statuses: `UNVERIFIED`, `SUPPORTED`, `MULTI_SOURCE_SUPPORTED`,
`CONTESTED`, `CONTRADICTED`, `INSUFFICIENT_EVIDENCE`. **Absence of evidence is
never confirmation.**

## Cross-source verification

Deterministic foundations (`DeterministicClaimVerifier`):

* **Source independence** — supporting sources are counted by distinct
  registered domain; syndicated duplicates (identical content fingerprints)
  are **not** independent confirmations. `number of sources != truth`: a claim
  repeated by ten copies of the same article is one confirmation;
* **Evidence coverage** — supporting vs contradicting evidence ratio;
* **Authority weighting** — government/academic/primary sources weigh more
  (documented, deterministic);
* **Confidence** — published formula:
  `coverage x (0.5 + 0.5 x independence) x (0.5 + 0.5 x authority)`;
* statuses: no evidence → `INSUFFICIENT_EVIDENCE`; contradicting only →
  `CONTRADICTED`; both sides → `CONTESTED`; ≥2 independent sources →
  `MULTI_SOURCE_SUPPORTED`; one → `SUPPORTED`.

## Contradiction detection

For each claim's (subject, predicate) group:

* **conflicting numbers / dates** — extracted values that differ;
* **explicit disagreement** — passages with disagreement language ("however",
  "contrary to", "disputed", "according to … but", "conflicting reports");
* both sides and their source provenance are **preserved**; the claim is
  marked `CONTESTED`/`CONTRADICTED`; the conflict is recorded with type, values,
  sources, and `resolution_status: unresolved`. **The system never silently
  chooses a side and never invents a resolution.** When a stronger-authority
  source can be justified it may be noted — but the conflict stays recorded.

## Research report

The `research_report` artifact contains: research question, opportunity
reference, executive findings, verified claims, contested claims, unresolved
questions, evidence map, source list with quality indicators, contradiction
summary, confidence summary, limitations, timestamps, and lineage
(`research_plan_id`, opportunity id). **Every major factual statement is
traceable to evidence** (claim → evidence refs → source ids). The Phase 0
report fields (topic, summary, simple sources/claims/contradictions) are
populated alongside the rich fields for backward compatibility. The report is
the input contract for the future Script Engine.

## Cost / quota control

* **Source deduplication** — canonical URL fingerprinting (scheme/host
  normalization, tracking-param stripping, query sorting);
* **Content fingerprinting** — sha256 of normalized content for syndication
  detection;
* **Caching** — the cross-job `source_cache` table (TTL, hit counting,
  bounded content) means a source is never collected twice across research
  jobs;
* **Limits** — maximum sources per job by depth, maximum collection size,
  evidence-per-source cap, retry limits, configurable depth;
* **Usage tracking** — every discovery/collection call records provider,
  endpoint, latency, success/failure, and metadata (never credentials).

## Limitations

1. **REAL external connectivity: NOT RUN — owner test pending.** Discovery is
   verified against a realistic local simulation of the documented search API
   and sources (`tests/simulated/research_server.py`); the live owner-run
   against `https://en.wikipedia.org` (and any future real source provider)
   has not been performed. `SIMULATED RESEARCH TEST: PASS`.
2. Extraction and verification are deterministic and auditable, but not
   semantically deep — the `EvidenceExtractor`/`ClaimVerifier` contracts are
   the seams for LLM-backed or learned implementations.
3. DNS-rebinding TOCTOU window (documented above).
4. Only the configured search API's sources are available; source breadth
   depends on the provider.

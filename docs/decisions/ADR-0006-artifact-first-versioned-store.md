# ADR-0006: Artifact-first, versioned artifact store

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

The brief requires that every major stage produce a structured artifact, that
artifacts are versioned, and that important pipeline outputs are never
silently overwritten. Unstructured text must not flow between modules.

## Decision

* Every stage output is a **typed artifact** with a versioned contract
  (pydantic model + JSON Schema file, kept in sync by tests).
* The **ArtifactStore** (`factory/storage/artifacts.py`) persists payloads as
  JSON files under a configured root, indexed by an `artifacts` table row.
* **Immutability/versioning:** every save creates a new id + storage path
  with the next version number for its lineage (`project_id` + `type` +
  `lineage_key`, defaulting to the producing `job_id`). Nothing is ever
  overwritten; writes are atomic (temp + rename); reads verify a SHA-256
  checksum.
* **Containment:** storage references are validated against the root (no
  absolute paths, no `..`, no escapes).
* Entities that are primarily pipeline outputs (Script, Scene, Shot,
  Timeline, QAReport, ContentBrief, ...) are **artifact-first**: their
  canonical form is the artifact; the database holds only lightweight link
  rows where useful.

## Consequences

* Positive: pipeline stages are contract-driven; outputs are auditable and
  reproducible; regeneration produces a new version, never a silent overwrite;
  integrity is verified on read; storage is filesystem-friendly (cheap,
  inspectable, easy to back up or move to object storage later by changing
  the root).
* Negative: two representations of contracts (pydantic + JSON Schema) to
  keep in sync — mitigated by cross-validation tests for every type.

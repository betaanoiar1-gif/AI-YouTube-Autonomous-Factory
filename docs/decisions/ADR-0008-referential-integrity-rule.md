# ADR-0008: Referential integrity rule (FK ownership vs logical references)

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

The schema links entities in many ways: ownership (a channel belongs to a
project), history (usage records reference jobs), and pipeline outputs
(artifacts reference jobs; jobs reference artifacts). Blanket FK constraints
create circular dependencies (artifacts ↔ jobs) and make historical records
hostage to entity deletion (usage history should survive a job row being
purged).

## Decision

* **FK constraints model ownership within a plane:** project → channels,
  videos, metrics, topics, clusters, opportunities, sources, documents,
  claims, jobs, events; niche → projects; channel → videos/metrics; source →
  documents; document → claims; opportunity → briefs; cluster → topics.
* **Cross-plane and observability references are plain indexed columns:**
  `artifacts.project_id`, `artifacts.job_id`,
  `pipeline_jobs.input_artifact_id`, `pipeline_jobs.output_artifact_id`,
  `content_briefs.artifact_id`, `provider_usage.job_id`, `assets.project_id`.
* No circular FKs exist; deletion of a project/job never destroys history or
  artifacts.

## Consequences

* Positive: simple, portable migrations; observability data is append-only
  and durable; the artifact store is the source of truth for payloads.
* Negative: logical references are not DB-enforced (application-level
  integrity for those links) — acceptable because they are references *to*
  durable artifacts, not ownership.

# ADR-0005: One pipeline_jobs table for all typed jobs

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

The product brief lists typed jobs (DISCOVERY_JOB, ANALYSIS_JOB, RESEARCH_JOB,
SCRIPT_JOB, PRODUCTION_JOB, QA_JOB, ...) and requires resumable jobs with
states, progress, error info, retry info, and artifact references.

## Decision

Realize **all** typed jobs as rows in a single `pipeline_jobs` table with a
`type` discriminator (`JobType` enum), served by one `JobService`
(state machine, retries, checkpoints, events).

* Convenience constructors: `JobRecord.discovery(...)`, `JobRecord.render(...)`.
* The full pipeline (`DISCOVERY`, `ANALYSIS`, `OPPORTUNITY_DETECTION`,
  `RESEARCH`, `CONTENT_BRIEF`, `SCRIPT`, `VISUAL_PLAN`, `ASSET_ACQUISITION`,
  `VOICE_GENERATION`, `RENDER`, `QA`, `PUBLISH_PACKAGE`) maps onto it.
* Typed jobs are *views* over the same resumable machinery, not parallel
  implementations.

## Consequences

* Positive: one state machine to validate and test; one retry/checkpoint/
  event mechanism; scheduling and observability work uniformly across the
  pipeline; no schema duplication per job type.
* Negative: job-type-specific input fields live in the JSON `payload` (less
  relational purity) — acceptable because each stage's *output* is a typed
  artifact, and inputs are validated at the service boundary.

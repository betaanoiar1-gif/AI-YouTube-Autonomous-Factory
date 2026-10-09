# Domain model

The initial domain model covers all entities required by the architecture.
It deliberately contains **only fields justified by the current
architecture** — no speculative fields.

Two representations exist and are kept consistent:

* **Relational entities** — pydantic models in `factory/domain/models.py`,
  persisted as SQLAlchemy tables in `factory/storage/models.py` (initial
  schema in Alembic migration `233f9e042722`).
* **Artifact-first entities** — pipeline outputs whose canonical form is a
  versioned artifact (typed contract in `factory/schemas/artifacts.py` +
  JSON Schema in `schemas/artifacts/`).

Typed jobs (`DiscoveryJob`, `RenderJob`, ...) are **not** separate tables:
they are rows in the single `pipeline_jobs` table with a `type`
discriminator (see `docs/job-system.md`).

## Entity map

### Control plane

| Entity | Kind | Notes |
| --- | --- | --- |
| `Project` | relational | niche + language + target audience + publishing frequency + per-project `config` JSON + status |
| `Channel` | relational | tracked YouTube channel (`external_channel_id`, unique) |
| `Niche` | relational | name, description, keywords |
| `PipelineJob` | relational | the job system (see `docs/job-system.md`) |
| `JobEvent` | relational | durable structured per-job log events |
| `Provider` | relational | configured provider (non-secret config only) |
| `ProviderUsage` | relational | one observed provider request (see `docs/cost-control.md`) |

### Intelligence plane

| Entity | Kind | Notes |
| --- | --- | --- |
| `Video` | relational | discovered video (`external_video_id`, unique) |
| `VideoMetrics` | relational | point-in-time snapshot (views, likes, comments, avg view duration, CTR) |
| `ChannelMetrics` | relational | point-in-time snapshot (subscribers, total views, total videos) |
| `Topic` | relational | topic within a project |
| `TopicCluster` | relational | cluster of topics (label, summary) |
| `ContentOpportunity` | relational | title, angle, content gap, score (0–100), rationale, status workflow |

### Research plane

Phase 2 (see `docs/research-plane.md`) realizes the research plane as
**artifact-first contracts** — sources, evidence, claims, and reports are
versioned artifacts with typed contracts; the database holds only the
cross-job `source_cache` (dedup + fingerprinting). The Phase 0 relational
tables (`sources`, `research_documents`, `research_claims`) remain part of
the domain model; the pipeline's canonical data lives in artifacts.

| Entity | Kind | Notes |
| --- | --- | --- |
| `ResearchPlan` | **artifact-first** (`research_plan`) | plan derived from an opportunity: central question, subquestions, required facts, source/verification requirements |
| `SourceItem` | **artifact-first** (`source`) | url + fingerprints, justified type/authority, collection status (metadata only — never full content) |
| `EvidenceItem` | **artifact-first** (`evidence`) | extracted fact with bounded passage, location, confidence, structured subject/predicate/value |
| `VerifiedClaim` | **artifact-first** (`research_claim`) | claim with verification status, provenance, independent-source count |
| `ResearchReport` | **artifact-first** (`research_report`) | findings, verified/contested claims, evidence map, sources + quality, contradictions, confidence summary, limitations, lineage |
| `SourceCacheEntry` | relational (cross-job cache) | collected source content (bounded) + fingerprints + TTL + hit count |
| `Source` / `ResearchDocument` / `ResearchClaim` | relational (Phase 0 model) | domain tables retained; pipeline data lives in artifacts |

### Content plane

| Entity | Kind | Notes |
| --- | --- | --- |
| `ContentBrief` | **artifact-first** (`content_brief`) + link row (`content_briefs`) | canonical payload is a versioned artifact; the row links it to its opportunity and tracks workflow status |
| `Script` | **artifact-first** (`script`) | scenes → shots, narration, durations |
| `Scene` | part of `script` artifact | narration + visual description + shots |
| `Shot` | part of `script`/`storyboard` artifacts | type, description, duration, asset requirements/hints |
| `Storyboard` | **artifact-first** (`storyboard`) | the visual/shot plan derived from a script |

### Production plane

| Entity | Kind | Notes |
| --- | --- | --- |
| `Asset` | relational | media asset: type, storage ref, checksum, source (generated/stock/purchased/recorded), license info, duration |
| `Timeline` | **artifact-first** (`production_timeline`) | tracks (video/audio/voice/music/subtitle/graphic) with clips |
| `RenderJob` | `PipelineJob` with `type=render` | the render job (output: rendered video asset) |

### Quality plane

| Entity | Kind | Notes |
| --- | --- | --- |
| `QAReport` | **artifact-first** (`qa_report`) | per-category checks (factual/narrative/visual/audio/subtitle/technical), overall status |

### Publishing plane

| Entity | Kind | Notes |
| --- | --- | --- |
| `PublishPackage` | **artifact-first** (`publish_package`) | title + title options, description, tags, category, language, thumbnail concept, schedule |

### Discovery

| Entity | Kind | Notes |
| --- | --- | --- |
| `DiscoveryJob` | `PipelineJob` with `type=discovery` | the discovery job (output: `discovery_result` artifact) |
| `DiscoveryResult` | **artifact-first** (`discovery_result`) | videos found for a query with metrics snapshots + quota used |

### Analysis

| Entity | Kind | Notes |
| --- | --- | --- |
| `AnalysisResult` | **artifact-first** (`analysis_result`) | aggregate metrics + content patterns |
| `OpportunityList` | **artifact-first** (`opportunity_list`) | ranked opportunities with scores, rationale, content gaps |
| `ResearchReport` | **artifact-first** (`research_report`) | sources, claims, contradictions, summary |

## Design decisions

1. **Typed jobs are one table.** `DISCOVERY_JOB`, `ANALYSIS_JOB`,
   `RESEARCH_JOB`, `SCRIPT_JOB`, `PRODUCTION_JOB` (render), `QA_JOB`, etc. are
   `pipeline_jobs` rows with a `type` discriminator. One resumable job system
   (state machine, retries, checkpoints, events) instead of N parallel ones.
   Convenience constructors: `JobRecord.discovery(...)`, `JobRecord.render(...)`.
2. **Artifact-first entities.** Entities whose value is a pipeline output
   (brief, script, timeline, QA report, ...) are versioned artifacts; the
   database holds a lightweight index row only where a relational link is
   useful (e.g. `content_briefs.opportunity_id`).
3. **Metrics are snapshots.** `video_metrics`/`channel_metrics` rows are
   point-in-time snapshots (`snapshot_at`), never overwritten — performance
   history is append-only.
4. **Referential integrity rule.** FK constraints model ownership within a
   plane (project → channels/videos/metrics/topics/opportunities/sources/
   documents/claims/jobs/events; niche → projects; channel → videos/metrics;
   source → documents; document → claims; opportunity → briefs; cluster →
   topics). Cross-plane and observability references (`artifacts.project_id`,
   `artifacts.job_id`, `pipeline_jobs.*_artifact_id`,
   `content_briefs.artifact_id`, `provider_usage.job_id`,
   `assets.project_id`) are plain indexed columns so historical records
   survive entity deletion and no circular FKs exist.
5. **Statuses are strings** validated in the pydantic layer (portable
   migrations, no SQLAlchemy Enum DDL).
6. **IDs are UUID hex strings**; timestamps are UTC, timezone-aware.

## ER summary (relational core)

```
projects ─┬─ channels ─┬─ videos ── video_metrics
          │            └─ channel_metrics
          ├─ topics ── topic_clusters
          ├─ content_opportunities ─┬─ sources ── research_documents ── research_claims
          │                         └─ content_briefs (→ artifact)
          ├─ pipeline_jobs ── job_events
          └─ (logical refs) artifacts, assets, provider_usage
niches ── projects
providers (registry)
```

# Job system

The entire application is designed around **resumable jobs**. Every pipeline
stage (discovery, analysis, research, script, production, QA, publishing) runs
as a job; jobs are the unit of work, retry, checkpointing, and observability.

## Contracts

### Job types (`factory/jobs/types.py`)

The product brief's typed jobs map onto one discriminator enum:

| Brief name | `JobType` | Output artifact |
| --- | --- | --- |
| DISCOVERY_JOB | `YOUTUBE_DISCOVERY` | `discovery_result` |
| ANALYSIS_JOB | `MARKET_ANALYSIS` | `analysis_result` |
| — | `OPPORTUNITY_DETECTION` | `opportunity_list` |
| RESEARCH_JOB | `RESEARCH` | `research_report` |
| — | `CONTENT_BRIEF` | `content_brief` |
| SCRIPT_JOB | `SCRIPT` | `script` |
| — | `VISUAL_PLAN` | `storyboard` |
| — | `ASSET_ACQUISITION` | (assets) |
| — | `VOICE_GENERATION` | (audio assets) |
| PRODUCTION_JOB (RenderJob) | `RENDER` | `production_timeline` → rendered video |
| QA_JOB | `QA` | `qa_report` |
| — | `PUBLISH_PACKAGE` | `publish_package` |

One table (`pipeline_jobs`) + one enum instead of N parallel job systems.

### Job record

A job contains (mirrored by the `JobRecord` pydantic contract and the
`pipeline_jobs` table):

* `id` (UUID), `type`, `status`
* `project_id`, `idempotency_key`
* `progress` (0–100)
* `attempts`, `max_retries`
* `payload` (input configuration — never secrets)
* `checkpoint` (resumability state written by the handler)
* `input_artifact_id`, `output_artifact_id` (artifact references)
* `error` (`{type, message, retryable, at}` — sanitized)
* `next_retry_at` (retry scheduling)
* `created_at`, `updated_at`, `started_at`, `finished_at`

### Statuses and transitions

States: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`.

```
                 claim            complete
PENDING ───────────────► RUNNING ──────────► SUCCEEDED (terminal)
   ▲                     │  │
   │               fail  │  │ fail (retries exhausted / non-retryable)
   │                     ▼  ▼
   │                   FAILED ◄── (terminal, restartable)
   │                     ▲
   └──── restart ────────┘
   ▲
   └──── restart ──── CANCELLED (terminal, restartable)
```

Allowed transitions (enforced by `JobService`):

* `PENDING → RUNNING | CANCELLED`
* `RUNNING → SUCCEEDED | FAILED | CANCELLED`
* `FAILED → PENDING` (restart)
* `CANCELLED → PENDING` (restart / re-queue)
* `SUCCEEDED →` *(terminal)*

Invalid transitions raise `InvalidJobTransitionError`.

## Phase 1 pipeline handlers

`factory/intelligence/pipeline.py` wires the intelligence engines to the job
system as handlers (`IntelligencePipeline.handlers`):

* `YOUTUBE_DISCOVERY` → `DiscoveryEngine` — paginated search → video metrics →
  channel metrics → `discovery_result` artifact (checkpointed per stage);
* `MARKET_ANALYSIS` → `AnalysisEngine` — loads the discovery artifact (never
  re-runs discovery) → `analysis_result` artifact;
* `OPPORTUNITY_DETECTION` → `OpportunityEngine` — loads the analysis artifact
  + deterministic clustering → `opportunity_list` artifact.

Phase 3A adds the deterministic content pipeline (`factory/content/pipeline.py`):

* `CONTENT_BRIEF` → loads the selected opportunity and its matching `research_report`, saves an evidence-traceable `content_brief`, then a `narrative_outline`. It uses the existing artifact store and resumable job context, performs no network calls, and requires no credentials. The job's output artifact is the narrative outline; both artifacts share the job lineage.

Phase 2 adds the research pipeline (`factory/research/pipeline.py`):

* `RESEARCH` → `ResearchEngine` — loads the `opportunity_list` artifact and
  runs 8 checkpointed stages (PLAN → SOURCE_DISCOVERY → SOURCE_COLLECTION →
  EVIDENCE_EXTRACTION → CLAIM_BUILDING → VERIFICATION → CONTRADICTION_ANALYSIS
  → REPORT) → `research_plan` + `research_report` artifacts. Every stage
  checkpoints; a failed later stage never repeats completed earlier stages.

Run a single stage or the whole chain from the CLI:
`factory pipeline run --job-type youtube_discovery --project-id … --payload …`
or `factory pipeline run-chain --project-id … --payload …`. The CLI accepts
`--job-type research` for the research job.

## Semantics

* **Claiming** (`JobService.claim`) atomically moves `PENDING → RUNNING` and
  increments `attempts` — safe for a future multi-worker deployment.
* **Retries** (`fail`): a retryable failure with attempts remaining returns
  the job to `PENDING` with `next_retry_at = now + min(base * 2^(attempts-1),
  max)` (configurable via `JOB_RETRY_BASE_SECONDS` / `JOB_RETRY_MAX_SECONDS`).
  When retries are exhausted (or the error is non-retryable) the job becomes
  `FAILED` with sanitized error info. Total runs = `1 + max_retries`.
* **Restart** (`restart`): `FAILED`/`CANCELLED → PENDING`; attempts are
  preserved (they are the retry budget), error info and retry scheduling are
  cleared. Completed work is never corrupted: the checkpoint and any output
  artifacts persist.
* **Checkpoints** (`save_checkpoint`/`load_checkpoint`): handlers merge a
  checkpoint dict into the job row between attempts. A restarted job resumes
  from the checkpoint — this is the resumability mechanism. Checkpoints are
  part of the job contract, so *every* stage must checkpoint its progress.
* **Progress** (`update_progress`): 0–100, validated.
* **Idempotent enqueue**: `enqueue(..., idempotency_key=...)` returns the
  existing active job instead of creating a duplicate (unique constraint on
  `idempotency_key`).
* **Cancellation** (`cancel`): `PENDING`/`RUNNING → CANCELLED`; a running
  handler can also raise `JobCancelledError`.
* **Observability**: lifecycle transitions are written as durable
  `job_events` rows (level, message, context) and mirrored to the structured
  log with `job_id` context bound.

## Runner

`factory/jobs/runner.py::run_job(service, job_id, handler)` executes a job:

1. claim the job (single-claim semantics);
2. build a `JobContext` (job record + loaded checkpoint + controls);
3. invoke `handler(context) → output_artifact_id | None`;
4. on success → `complete`; on `JobCancelledError` → `cancel`; on any other
   exception → `fail` (with retry scheduling).

`JobContext` exposes `save_checkpoint(...)`, `set_progress(...)`, and the
current job record. A later phase can move `run_job` into a dedicated worker
process without changing handlers — the contract is the context + checkpoint.

## Retry due query

`JobService.due_for_retry(now)` returns `PENDING` jobs whose `next_retry_at`
has arrived — the hook a future scheduler/worker loop uses to re-claim them.

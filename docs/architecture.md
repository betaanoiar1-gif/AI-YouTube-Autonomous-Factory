# Architecture

**Project:** AI YouTube Autonomous Factory
**Phase:** 0 — Architecture & Foundation
**Status:** Foundation complete. The live CleanAPIs connectivity verification is
blocked in the current sandbox (see `docs/cleanapis.md` § Connectivity test).

## Product vision

An autonomous AI YouTube content factory. The user provides a niche, language,
target audience, and publishing frequency; the system discovers successful
long-form videos in the niche, analyzes them, detects opportunities and
content gaps, researches them, and produces **original** content (brief →
script → storyboard → assets → voice → timeline → render → QA → publish
package). The system must understand the market and produce original content
— never copy or rewrite competitor videos.

## Planes

The system is organized into seven planes. Phase 0 builds the shared
foundation (control plane storage, job system, artifact system, provider
abstraction, observability); later phases fill in plane-specific engines.

| Plane | Responsibility | Phase 0 state |
| --- | --- | --- |
| **Control** | projects, channels, niches, configuration, jobs, scheduling, pipeline state, user settings | Domain model + storage + job system |
| **Intelligence** | YouTube discovery, metadata collection, channel/performance analysis, topic clustering, trend detection, success scoring, opportunity detection | `DiscoveryProvider` contract only (Phase 1) |
| **Research** | source discovery/collection, evidence extraction, fact management, contradiction detection, research reports | Domain model + artifact contract |
| **Content** | opportunities, briefs, narrative structures, scripts, claims, citations, visual planning | Domain model + artifact contracts |
| **Production** | asset management, voice, audio, timeline, editing, rendering, export | Domain model + artifact contracts; `TTSProvider`/`VideoProvider`/`ImageProvider` contracts |
| **Quality** | factual/narrative/visual/audio/subtitle/technical QA | `QAReport` artifact contract; `VisionProvider`/`ASRProvider` contracts |
| **Publishing** | title generation/scoring, descriptions, metadata, thumbnail concepts, publishing preparation | `PublishPackage` artifact contract |

## Layered structure

```
┌────────────────────────────────────────────────────────────┐
│  API / CLI / Workers (entry points)                        │
│  factory/api (FastAPI health), factory/cli, job runner     │
├────────────────────────────────────────────────────────────┤
│  Application services                                      │
│  JobService, ArtifactStore, provider wiring, health        │
├────────────────────────────────────────────────────────────┤
│  Domain                                                │
│  factory/domain (pydantic entities), factory/schemas       │
│  (typed artifact contracts)                                │
├────────────────────────────────────────────────────────────┤
│  Providers (abstractions + implementations)                │
│  factory/providers/base (interfaces)                       │
│  factory/providers/cleanapis (real LLM provider)           │
├────────────────────────────────────────────────────────────┤
│  Infrastructure                                            │
│  factory/storage (SQLAlchemy/SQLite, artifact store),      │
│  factory/observability (logging, usage), factory/security  │
├────────────────────────────────────────────────────────────┤
│  Configuration (factory/config)                            │
│  app settings / provider settings / runtime secrets (env)  │
└────────────────────────────────────────────────────────────┘
```

**Dependency rule:** business logic depends on provider *interfaces*
(`factory.providers.base`) and domain/artifact contracts — never on CleanAPIs
specifics, never on the database directly outside the storage layer.

## Key architectural principles

1. **Every major stage produces a structured, versioned artifact** (typed
   contract), not unstructured text passed between modules.
   See `docs/artifact-system.md`.
2. **Everything runs as a resumable job** with a validated state machine,
   retries, and checkpoints. See `docs/job-system.md`.
3. **Provider boundary:** `LLMProvider` (and the other provider interfaces)
   isolate all vendor specifics. CleanAPIs is the real Phase 0 LLM provider;
   other providers are contracts only. See `docs/provider-system.md`.
4. **Normalized AI requests:** `LLMRequest`/`LLMResponse` are the only shapes
   business logic sees; the provider translates to/from the wire format.
5. **Cost control is foundational:** caching/deduplication, per-job request
   budgets, token accounting, and provider usage tracking are built in from
   day one. See `docs/cost-control.md`.
6. **Observability is foundational:** structured JSON logs with mandatory
   secret redaction, durable job events, and provider usage records.
7. **Security is foundational:** secrets only from the environment, path
   traversal protection, controlled subprocess execution, safe URL handling,
   error sanitization, and log redaction. See `docs/decisions/ADR-0007-secret-handling.md`.
8. **Configuration is layered:** runtime secrets (env) ≠ provider config ≠
   application config ≠ project config (per-project JSON in the database).

## Repository layout

```
factory/                 # the application package
  config/                # settings: app vs provider vs runtime secrets
  domain/                # pydantic domain models (relational entities)
  schemas/               # artifact contracts (pydantic) + registry
  providers/             # provider interfaces + CleanAPIs implementation
    base.py              # LLMProvider, ASRProvider, VisionProvider, ...
    llm/                 # normalized LLMRequest/LLMResponse model
    cleanapis/           # client, provider, connectivity test
    cache.py, budget.py  # cost-control foundation
  storage/               # SQLAlchemy models, engine, artifact store
  jobs/                  # job types, service (state machine), runner
  observability/         # structured logging, provider usage tracking
  security/              # redaction, safe paths/URLs/subprocess
  api/                   # FastAPI app (health check)
  cli.py                 # health / db / cleanapis / serve
schemas/artifacts/       # published JSON Schema contracts (one per artifact type)
alembic/                 # database migrations
tests/                   # unit (mocked) + integration (live, marked)
docs/                    # this documentation + ADRs
```

## Data flow (Phase 0 → Phase 1)

```
User config (niche, language, audience, frequency)
  → Project row (+ per-project config JSON)
    → PipelineJob(type=DISCOVERY)          [Phase 1: DiscoveryProvider → YouTube]
      → Artifact(discovery_result)         [versioned, typed]
        → PipelineJob(type=ANALYSIS)
          → Artifact(analysis_result)
            → PipelineJob(type=OPPORTUNITY_DETECTION)
              → Artifact(opportunity_list) → ContentOpportunity rows
                → ... research → brief → script → storyboard
                → ... production → timeline → QA → publish_package
```

Each arrow is a job boundary; each noun is a typed artifact. A failed job
restarts from its checkpoint without corrupting completed artifacts.

## Technology stack

See `docs/decisions/ADR-0001-technology-stack.md` for the full rationale.

* **Language:** Python 3.11 (mature AI/ML ecosystem, strong typing, runs locally)
* **API:** FastAPI + uvicorn (health check now; control API later)
* **Persistence:** SQLAlchemy 2.0 ORM + SQLite (Phase 0) + Alembic migrations
* **Contracts:** pydantic v2 (runtime validation) + JSON Schema 2020-12
  (published contracts), kept in sync by tests
* **HTTP (provider calls):** httpx (timeouts, testability via MockTransport)
* **Testing:** pytest (unit with mocked HTTP; live integration tests marked
  `live` and excluded by default)
* **Quality gates:** ruff (lint + format), mypy (type checking)

## What Phase 0 deliberately does NOT include

* Full YouTube discovery engine (Phase 1 — `DiscoveryProvider` contract only)
* Opportunity/research/script engines (contracts + storage only)
* Video rendering, dashboard, autonomous scheduling loop
* Any scraping, undocumented endpoints, or quota evasion — discovery will use
  the documented YouTube Data API with observable quota consumption

# AI YouTube Autonomous Factory

An autonomous AI YouTube content intelligence and production platform.

The user provides a **niche**, **language**, **target audience**, and
**publishing frequency**; the system discovers successful long-form videos in
the niche, analyzes them, detects opportunities and content gaps, researches
them, and produces **original** content — brief → script → storyboard →
assets → voice → timeline → render → QA → publish package — as an autonomous,
resumable production pipeline.

> The system understands the market and produces original content. It never
> copies or rewrites competitor videos.

## Phase 0 status

Phase 0 (architecture & foundation) is complete:

* ✅ Project skeleton, architecture documentation, domain model
* ✅ Initial database schema + Alembic migrations (SQLite, PostgreSQL-ready)
* ✅ Artifact contracts (pydantic + JSON Schema, 10 artifact types)
* ✅ Resumable job system (state machine, retries, checkpoints)
* ✅ Provider interfaces (`LLMProvider`, `ASRProvider`, `VisionProvider`,
  `EmbeddingProvider`, `ImageProvider`, `VideoProvider`, `TTSProvider`,
  `DiscoveryProvider`)
* ✅ **Real CleanAPIs provider** (`CleanAPIsProvider` + thin verified HTTP
  client) behind the `LLMProvider` boundary
* ✅ Configuration system (app / provider / project / runtime secrets)
* ✅ Logging foundation (structured JSON + mandatory secret redaction)
* ✅ Provider usage tracking foundation
* ✅ Testing foundation (unit + **simulated/offline CleanAPIs integration**
  + owner-run live test, fully separated)
* ✅ `.env.example`, basic health check (CLI + HTTP), ADRs
* ✅ **Simulated CleanAPIs test: PASS** — the production
  `CleanAPIsProvider` code path is verified end-to-end against a realistic
  local HTTP simulation of the CleanAPIs API (no credentials, no real
  network). See `docs/testing-strategy.md`.
* ⏳ **Real CleanAPIs connectivity: NOT RUN — owner test pending** — the live
  test against `https://cleanapis.com` is implemented and reserved for a
  manual owner-run with a real key (`make connectivity`). It was not run in
  the build sandbox (no key in env; egress blocked). See `docs/cleanapis.md`.

**Phase 1 (intelligence plane) is also complete:** production
`YouTubeDiscoveryProvider` (YouTube Data API v3 only), the
`YOUTUBE_DISCOVERY` → `MARKET_ANALYSIS` → `OPPORTUNITY_DETECTION` pipeline
(deterministic, resumable, quota-aware), extended artifact contracts, and a
realistic offline YouTube simulation for automated tests. See
`docs/intelligence-plane.md` and `docs/phase-1-final-report.md`.

```bash
# Run the intelligence pipeline (owner, with a real YouTube API key)
python -m factory.cli pipeline run-chain --project-id proj-1 \
  --payload '{"query": "forgotten tunnels", "language": "en", "result_limit": 50}'
```

Not in Phases 0-1 (by design): research, script, visual, production,
rendering, QA, publishing, dashboard, autonomous scheduling.

## Quickstart

```bash
# 1. Set up (Python 3.11+)
make setup            # creates .venv and installs dependencies

# 2. Configure (never commit .env)
cp .env.example .env  # then edit: set cleanapis_API_KEY=cc_...

# 3. Verify
make health           # local health check (CLI)
make migrate          # apply database migrations
make test             # full automated suite (unit + simulated CleanAPIs)
make test-simulated   # ONLY the simulated/offline CleanAPIs integration tests
make lint             # ruff
make typecheck        # mypy

# 4. Verify the REAL CleanAPIs connection — OWNER-RUN ONLY
#    (requires a real key + network egress; never part of automated testing)
make connectivity     # python -m factory.cli cleanapis test-connection
# or: make test-live  # pytest -m live tests/integration -v

# 5. Run the API (health check)
make serve            # http://localhost:8080/health
```

## Documentation

| Document | Contents |
| --- | --- |
| `docs/architecture.md` | planes, layers, data flow, stack, layout |
| `docs/domain-model.md` | entities, artifact-first vs relational, ER summary |
| `docs/job-system.md` | job types, states, retries, checkpoints, runner |
| `docs/provider-system.md` | provider interfaces, normalized LLM model, structured output |
| `docs/artifact-system.md` | artifact contracts, versioning, immutability, integrity |
| `docs/testing-strategy.md` | layers, required coverage map, commands, quality gates |
| `docs/cost-control.md` | caching, usage tracking, budgets, model selection, quotas |
| `docs/cleanapis.md` | **verified** CleanAPIs facts, configuration, errors, connectivity test |
| `docs/phase-0-final-report.md` | **Phase 0 final report** (simulated vs real connectivity status) |
| `docs/intelligence-plane.md` | **Phase 1**: DiscoveryProvider, analysis methodology, scoring, clustering, opportunities, quota strategy |
| `docs/phase-1-final-report.md` | **Phase 1 final report** (simulated vs real YouTube API status) |
| `docs/decisions/` | Architecture Decision Records (ADR-0001 … ADR-0009) |

## Security rules (always in force)

* Never hard-code, commit, log, or print API keys. The application loads
  `cleanapis_API_KEY` from the environment only.
* `.env` is gitignored; `.env.example` contains placeholders only.
* Logs are structured JSON with mandatory secret redaction (registered
  secrets + `cc_`/`Bearer`/assignment patterns).
* Artifact storage is path-traversal protected; subprocesses are
  shell-free and allow-listed; URLs are validated (no embedded credentials).

## Repository layout

```
factory/            application package (config, domain, schemas, providers,
                    storage, jobs, observability, security, api, cli)
schemas/artifacts/  published JSON Schema artifact contracts
alembic/            database migrations
tests/              unit (mocked) + integration (live, marked)
docs/               documentation + ADRs
```

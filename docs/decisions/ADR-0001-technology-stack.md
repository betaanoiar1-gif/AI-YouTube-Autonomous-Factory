# ADR-0001: Technology stack

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

Phase 0 must establish a production-grade foundation that can run locally,
type-check and test cleanly, and deploy later without rewrites. The system
will orchestrate AI providers, manage media artifacts, and run resumable
batch jobs.

## Decision

* **Language: Python 3.11.** Mature ecosystem for AI/ML orchestration
  (pydantic, SQLAlchemy, httpx, pytest), strong typing support, runs
  everywhere, and matches the team's operational comfort. (Node 22 is also
  available in the environment, but the Python AI/data ecosystem is the better
  fit for artifact-heavy pipelines.)
* **API: FastAPI + uvicorn.** Mature, async-capable, automatic OpenAPI
  docs; Phase 0 exposes the health check, later phases expose the control
  API.
* **Persistence: SQLAlchemy 2.0 ORM + SQLite + Alembic.** Zero-infrastructure
  local operation (single file, easy backups), full ORM productivity,
  Alembic for versioned migrations. The URL is configurable
  (`DATABASE_URL`), so PostgreSQL is a drop-in change later.
* **Contracts: pydantic v2 + JSON Schema 2020-12.** pydantic for runtime
  validation; JSON Schema files as published, language-agnostic contracts;
  tests keep the two in sync.
* **HTTP for provider calls: httpx.** Explicit timeouts, and `MockTransport`
  makes the provider fully testable without network.
* **Testing: pytest.** Unit tests mock the HTTP layer; live tests are marked
  and separated.
* **Quality gates: ruff (lint+format) + mypy.** Fast, strict, single
  configuration in `pyproject.toml`.

## Consequences

* Positive: runs locally with `pip install -e ".[dev]"`; full type checking;
  migrations are versioned; contracts are testable; no heavy infrastructure.
* Negative: Python packaging boilerplate; SQLite is single-writer (fine for
  Phase 0–2; the schema and service layer are already PostgreSQL-compatible
  via `DATABASE_URL`).
* Alternatives considered: Node/TypeScript (weaker data/ORM ergonomics for
  this workload), no-SQL document store (loses relational integrity for the
  control plane), SDK-based provider clients (see ADR-0003).

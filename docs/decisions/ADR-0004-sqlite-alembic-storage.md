# ADR-0004: SQLite + Alembic storage

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

The system needs durable state for the control plane (projects, channels,
opportunities), the job system, artifacts index, and provider usage — with
versioned schema changes and zero infrastructure for local development.

## Decision

* **SQLite** as the Phase 0 database (single file, WAL mode, foreign keys
  ON, `check_same_thread=False` for the app's session-per-operation pattern).
* **Alembic** for schema migrations (`alembic/`, initial migration
  `233f9e042722`). `init_db` (metadata `create_all`) exists only as a
  test/dev convenience; production changes go through migrations.
* `DATABASE_URL` is configurable; the engine factory supports PostgreSQL
  URLs, so scaling up is a configuration change, not a rewrite.
* The artifact **payloads** live in the filesystem artifact store (see
  ADR-0006); the database holds the index.

## Consequences

* Positive: runs locally with no services; migrations are reviewable and
  reversible (downgrade tested); tests run against fresh temporary
  databases; no-drift test compares migrations to ORM metadata.
* Negative: SQLite is single-writer — fine for Phase 0/1 (single-process
  runner); the job system's atomic claim is already multi-worker-safe at the
  SQL level for when we move to PostgreSQL.

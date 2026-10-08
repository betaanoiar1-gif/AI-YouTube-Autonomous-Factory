# ADR-0007: Secret handling and redaction

**Status:** Accepted (Phase 0)
**Date:** 2026-10-08

## Context

The application holds runtime secrets (the CleanAPIs API key now; provider
keys, YouTube credentials, and signing secrets later). Secrets must never be
hard-coded, committed, logged, or exposed in errors — including accidentally,
via exception messages or URL echo.

## Decision

1. **Secrets come from the environment only** (`cleanapis_API_KEY`;
   `.env` for local dev, gitignored). Never in source, docs, fixtures, or
   reports.
2. **Configuration layers are separated:** runtime secrets (env) ≠ provider
   configuration (`CLEANAPIS_*`) ≠ application configuration (`APP_*`,
   `DATABASE_URL`, ...) ≠ project configuration (per-project JSON in the
   database).
3. **Redaction is mandatory and layered** (`factory/security/redaction.py`,
   `factory/observability/logging.py`):
   * known secret values are registered at startup and masked everywhere;
   * pattern-based masking catches `cc_...` keys, `Bearer ...` tokens, and
     `api_key = ...` / `token: ...` style assignments even when unregistered;
   * a logging filter redacts record messages/args/extras, and the JSON
     formatter re-redacts the final payload — a secret cannot reach a log
     sink even embedded in an exception message or context value.
4. **Errors are sanitized:** provider errors carry status/code/message from
   the provider envelope only; configuration errors never contain key
   material; database URLs are sanitized for logs (credentials stripped).
5. **Safe primitives:** `safe_join` (path traversal protection),
   `validate_http_url` (scheme allowlist, no embedded credentials),
   `safe_run` (subprocess without shell, executable allowlist, timeout,
   workspace-contained cwd) — the foundation later render/encode steps rely
   on.
6. **Tests prove it:** secret redaction in logs, no key material in usage
   records / health API responses / connectivity reports; repo-wide secret
   scan in the Phase 0 self-review.

## Consequences

* Positive: defense in depth; a single mistake (logging a config object,
  echoing a URL) does not leak credentials; tests lock the behavior in.
* Negative: redaction patterns can over-mask in pathological cases
  (acceptable trade-off for a content factory).

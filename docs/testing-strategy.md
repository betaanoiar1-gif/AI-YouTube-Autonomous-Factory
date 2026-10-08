# Testing strategy

## Layers

| Layer | Location | What it covers | Network / real key? |
| --- | --- | --- | --- |
| Unit | `tests/unit/` | configuration, security/redaction, CleanAPIs client & provider (mocked HTTP), LLM cache, budgets, job lifecycle, artifact store, artifact contracts, domain models, usage tracking, health API, migrations, database helpers | **No** — `httpx.MockTransport`; fake keys only |
| Integration (live) | `tests/integration/` | the REAL CleanAPIs connectivity test | **Yes** — marked `live` |

## Rules

1. **Ordinary tests mock CleanAPIs.** The HTTP layer is replaced with
   `httpx.MockTransport`; API keys in tests are fake values
   (`cc_test_fake_key_...`). The real key is never present in tests, fixtures,
   or logs (an autouse fixture deletes `cleanapis_API_KEY`/`CLEANAPIS_API_KEY`
   from the test environment and resets settings caches).
2. **The real CleanAPIs connection is used only in a clearly separated
   integration test** (`tests/integration/test_cleanapis_connectivity.py`),
   marked `live`. The default test run excludes it (`-m "not live"`); it is
   skipped unless `cleanapis_API_KEY` is set. It is reserved for a manual
   **owner-run** with a real key — automated testing of the provider path
   uses the simulated layer above instead. Run it explicitly with
   `pytest -m live tests/integration -v` or
   `python -m factory.cli cleanapis test-connection`.
3. **No test asserts on real model output content** — the live test asserts
   the verification steps (auth, endpoint, model availability, a minimal
   request succeeding, response parseability), not generated text.

## Required test coverage (from the Phase 0 brief) — where it lives

| # | Required test | Location |
| --- | --- | --- |
| 1 | Missing CleanAPIs key | `test_config.py` (safe `ConfigurationError`), `test_connectivity_offline.py` (step 1 fails cleanly) |
| 2 | Invalid CleanAPIs key | `test_cleanapis_client.py::test_401_maps_to_authentication_error` (mocked 401) |
| 3 | Successful CleanAPIs request | `test_cleanapis_client.py` (success + auth header), `test_cleanapis_provider.py` (normalized response) |
| 4 | CleanAPIs timeout | `test_cleanapis_client.py::test_timeout_maps_to_provider_timeout` |
| 5 | CleanAPIs HTTP error | `test_cleanapis_client.py` (402/403/404/422/429/502/500-retry mappings) |
| 6 | Malformed response | `test_cleanapis_client.py` (invalid JSON, missing choices, non-string content), `test_cleanapis_provider.py` (structured output: invalid JSON → correction retry → `StructuredOutputError`) |
| 7 | Provider usage recording | `test_usage_tracker.py`, `test_cleanapis_provider.py` (success + failure records, no key stored) |
| 8 | Secret redaction in logs | `test_logging_redaction.py`, `test_security.py` (registered secrets, `cc_` pattern, Bearer, assignment patterns, deep mapping) |

Plus: job lifecycle (states, transitions, retries, restarts, checkpoints,
resumability, idempotent enqueue, runner, events), artifact store (roundtrip,
versioning/immutability, integrity, traversal protection), artifact contracts
(pydantic ↔ JSON Schema cross-validation for all 10 types), domain models,
LLM cache (determinism, TTL, persistence, hit counting), budgets, health API
(incl. no-secret-leakage), and Alembic migrations (upgrade/downgrade/no-drift).

## Commands

```bash
make test           # full automated suite (unit + simulated; live deselected)
make test-simulated # ONLY the simulated/offline CleanAPIs integration tests
make test-live      # ONLY the live CleanAPIs connectivity test (owner-run, needs a real key)
make lint           # ruff check
make format         # ruff format
make typecheck      # mypy factory tests
make migrate        # alembic upgrade head
make health         # CLI health check
make connectivity   # CLI CleanAPIs connectivity test (owner-run, needs a real key)
```

## Quality gates (all must pass)

* `ruff check .` — lint (E/F/W/I/UP/B/SIM/RUF/S/C4/PTH/BLE)
* `ruff format --check .` — formatting
* `mypy factory tests` — type checking (strict-ish: disallow untyped defs,
  no implicit optional, warn on Any returns, pydantic plugin)
* `pytest` — full unit suite
* `alembic upgrade head` on a fresh database + `alembic check` (no drift)
* Secret scan: no `cc_[A-Za-z0-9_-]{12,}` keys or credential assignments
  anywhere in the repository (see Security below)

## Security testing

* `test_security.py` — path traversal (`..`, absolute, null bytes, separators),
  URL safety (scheme allowlist, no embedded credentials), controlled
  subprocess (allowlist, no shell, timeout, cwd containment), redaction.
* `test_logging_redaction.py` — registered secrets, `cc_` key pattern, Bearer
  tokens, `api_key = ...` assignment patterns, deep mapping redaction, and
  end-to-end "the JSON log line contains no secret".
* `test_health_api.py` / `test_usage_tracker.py` — API responses and usage
  records never contain key material.
* Manual sweep (Phase 0 self-review): `grep -rn "cc_[A-Za-z0-9_-]\{12,\}"`
  over the repo finds only fake test values; `.env` is gitignored and was
  never created with real values.

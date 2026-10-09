# ADR-0010: YouTube Data API v3 as the discovery boundary

**Status:** Accepted (Phase 1)
**Date:** 2026-10-08

## Context

Phase 1 (intelligence plane) needs YouTube discovery: search videos in a niche,
collect video/channel metrics, and feed market analysis. The product rules are
explicit: no scraping, no browser automation, no undocumented endpoints, no
quota circumvention; credentials only from environment/runtime secrets; the
provider stays behind the `DiscoveryProvider` interface.

## Decision

Implement the production `DiscoveryProvider` as `YouTubeDiscoveryProvider`
against the **documented YouTube Data API v3 only**:

* `GET /search` (part=snippet, video search, pagination), `GET /videos`
  (part=snippet,statistics,contentDetails), `GET /channels`
  (part=snippet,statistics) — batched by 50 ids per call;
* API-key auth via the documented `key` query parameter;
* the key comes only from `youtube_API_KEY` (environment), is registered with
  the log-redaction layer at startup, is additionally redacted by the `AIza…`
  pattern, and is never logged or persisted;
* HTTPS is required for the base URL (the API requires TLS); plain HTTP is
  allowed only for loopback hosts, which exist solely for the offline test
  simulation;
* only documented quota unit costs are counted (search=100, videos=1,
  channels=1); a configurable per-run budget is enforced before each call;
* the provider speaks provider-neutral normalized models
  (`factory/providers/discovery/types.py`) — never YouTube wire shapes.

## Consequences

* Positive: authorized, documented, quota-respecting discovery; the boundary
  is auditable; business logic is vendor-neutral; the offline simulation
  exercises the production code path end to end.
* Negative: the API's free daily quota (10,000 units default) bounds daily
  discovery volume — accepted (cost control is a feature, and the per-run
  budget plus caching keep spend minimal).

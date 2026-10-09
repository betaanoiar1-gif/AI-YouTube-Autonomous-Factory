# ADR-0014: SSRF-safe source collection

**Status:** Accepted (Phase 2)
**Date:** 2026-10-08

## Context

Source collection fetches arbitrary URLs discovered by a provider. That is the
classic SSRF surface: a discovered URL could point at localhost, private
networks, cloud metadata endpoints, or redirect there. The research plane must
retrieve sources safely or not at all.

## Decision

`SafeFetcher` (`factory/security/fetch.py`) is the single retrieval path:

* HTTPS by default; plain HTTP only for loopback hosts and only via an
  explicit constructor flag that exists solely for the offline test simulation
  (never environment-configurable);
* the ORIGINAL URL is validated before canonicalization (embedded credentials
  are rejected — canonicalization drops userinfo, so validating after it would
  miss them);
* the hostname is resolved and EVERY resolved IP is checked against blocked
  ranges: private, loopback, link-local, reserved, multicast, unspecified, and
  the RFC 6598 CGNAT range (with the loopback flag, only loopback is allowed);
* redirects are followed manually with each target re-validated, capped;
* timeout, streamed size cap, and a content-type allowlist;
* no shell execution, no filesystem access;
* sanitized errors (reason + credential-free URL);
* unretrievable sources keep their metadata and are marked `failed` — content
  is never invented.

Documented limitation: the IP check resolves before the request, leaving a DNS
rebinding TOCTOU window; full pinning would require a custom transport.

## Consequences

* Positive: the SSRF surface is closed at one choke point with tests for every
  blocked range, redirect case, and limit; providers cannot bypass it.
* Negative: a documented TOCTOU residual; the loopback flag must stay out of
  production configuration (enforced by design: it is a constructor parameter
  only).

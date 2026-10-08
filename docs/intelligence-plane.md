# Intelligence plane (Phase 1)

The first real pipeline: **Niche → YouTube Discovery → Video/Channel Metrics →
Market Analysis → Topic/Pattern Clustering → Opportunity Detection → versioned
artifacts.** Everything runs as resumable jobs producing typed artifacts.

```
Project (niche, language, target audience, frequency)
  → YOUTUBE_DISCOVERY job        → discovery_result artifact   (videos + channels + quota)
    → MARKET_ANALYSIS job        → analysis_result artifact   (per-video evidence + patterns)
      → OPPORTUNITY_DETECTION job → opportunity_list artifact (evidence-based opportunities)
```

Run it (owner, with a real key):

```bash
python -m factory.cli pipeline run-chain --project-id proj-1 \
  --payload '{"query": "forgotten tunnels", "language": "en", "result_limit": 50}'
```

## DiscoveryProvider and the YouTube API boundary

`DiscoveryProvider` (`factory/providers/base.py`) is the contract; the
production implementation is `YouTubeDiscoveryProvider`
(`factory/providers/youtube/`), using the **documented YouTube Data API v3
only**:

* `GET /search` (part=snippet, video search, pagination via `pageToken`);
* `GET /videos` (part=snippet,statistics,contentDetails, up to 50 ids/call);
* `GET /channels` (part=snippet,statistics, up to 50 ids/call);
* API-key auth via the documented `key` query parameter.

**Rules (enforced by design and tests):** no scraping, no browser automation,
no undocumented endpoints, no quota circumvention. The key comes only from
`youtube_API_KEY` (environment/runtime secret), is registered with the
redaction layer at startup, is never logged or persisted, and is redacted from
logs by pattern (`AIza…`) and by registration. Base URLs must be HTTPS (the
real API requires TLS); plain HTTP is allowed only for loopback hosts, which
exist solely for the offline test simulation.

The provider normalizes the wire format into provider-neutral models
(`factory/providers/discovery/types.py`): `DiscoveryQuery`,
`DiscoveredVideoItem`, `DiscoveredChannelItem`, `DiscoveryPage`. Business logic
never sees YouTube-specific shapes. Normalization parses ISO 8601 durations
(`PT1H2M3S` → seconds), string-delivered counts → ints, and RFC 3339
timestamps → timezone-aware datetimes. Only **metadata** is stored — never full
competitor content (descriptions are reduced to a character count).

**Error handling:** all HTTP errors map to typed, sanitized
`ProviderError` subclasses: `400 keyInvalid/keyRequired` →
`AuthenticationError`; other `400` → `ProviderValidationError`; `401` →
`AuthenticationError`; `403 quotaExceeded` → `QuotaExceededError`; other
`403` → `ScopeError`; `404` → `ResourceNotFoundError`; `429` →
`RateLimitError` (carries `Retry-After`); `5xx` → `ProviderHTTPError`;
timeout → `ProviderTimeoutError`; unreachable → `ProviderUnavailableError`;
unparseable bodies → `MalformedResponseError`. 429/5xx are retried with
exponential backoff + jitter honoring `Retry-After`.

## Discovery workflow (YOUTUBE_DISCOVERY)

Input (job payload): `query` (or `keywords`), `language`, `target_audience`,
`order`, `region_code`, `video_duration`, `published_after/before`,
`result_limit`, `page_size`.

Stages (each checkpointed for resumability):

1. **search** — paginate `search` until `result_limit` or exhaustion;
   deduplicate by video id across pages; the checkpoint carries the next
   page token, collected videos, and quota used;
2. **video_metrics** — `videos` lookups (batched by 50, cached per id);
3. **channel_metrics** — `channels` lookups for the distinct channel ids
   (cached per id).

Output: `discovery_result` artifact — query + context, search parameters,
normalized videos (id, channel, title, url, published_at, duration, views,
likes, comments, channel_title, description_chars, tags, category, definition),
channels (id, title, subscribers, views, videos), `quota_units_used`, and
provider metadata. A retried job resumes from its checkpoint: completed pages
are never re-fetched, and the provider cache makes repeat lookups free.

## Market analysis methodology (MARKET_ANALYSIS)

Input: a `discovery_result` artifact (by `input_artifact_id`, payload key, or
the latest for the project — **never re-runs discovery**).

Measurable signals per video (missing source data → the metric is `None`,
never invented):

| Signal | Formula |
| --- | --- |
| Age | `now - published_at` (days) |
| View velocity | `views / age_days` (None when age < 1 day) |
| Engagement rate | `(likes + comments) / views` (None when views == 0) |
| Comments per 1,000 views | `comments * 1000 / views` |
| Channel average | `channel view_count / channel video_count` |
| Channel-relative performance | `views / channel_average` |

Sub-scores are min-max normalized across the analyzed sample (0..1).
**Composite score** (0..100) — NOT "highest views = best":

```
composite = 100 * Σ(weight_i * sub_score_i) / Σ(weight_i)   over AVAILABLE sub-scores
weights: performance 0.30 · velocity 0.25 · engagement 0.20 ·
         channel_relative 0.15 · recency 0.10
```

Weights are renormalized over the available sub-scores when a metric is
unavailable. The artifact carries per-video evidence (all metrics +
sub-scores + composite), aggregates (total/mean/median views, channel count),
competition stats (per-channel views, top-channel share), the scoring
documentation (weights + formulas), and deterministic patterns (topics,
formats, questions). The analysis is reproducible: same input + same
timestamp → identical output.

## Clustering approach (deterministic, provider-independent)

`factory/intelligence/clustering.py` — **no LLM dependency** (ADR-0011). A
future embedding/LLM clusterer can implement the `Clusterer` protocol and
replace `DeterministicClusterer` without changing callers.

* **Topics:** recurring unigrams + bigrams from titles (stopword-filtered),
  frequency ≥ threshold, deterministic order (frequency desc, term asc);
* **Clusters:** connected components over videos sharing a recurring topic;
  deterministic ids (sha256 of members);
* **Questions:** titles that are questions (question-word starts or `?`);
* **Formats:** duration buckets + title format patterns (numbers, brackets,
  `vs`, `|`, `?`, `:`);
* **Underserved themes:** recurring topics whose average views are below
  `underserved_ratio` (0.5) × the sample median — a real gap, not noise;
* **Saturation:** per topic — `underserved` / `balanced` / `saturated`
  (saturated = covers ≥ 50% of the sample AND average ≥ median).

## Opportunity model (OPPORTUNITY_DETECTION)

Input: an `analysis_result` artifact + the clustering over its videos.

Each opportunity contains: `opportunity_id` (deterministic uuid5 of
project+topic), `topic`, `audience_question` (generated from the topic —
never quoted from a competitor), `evidence_refs`, `supporting_video_ids`,
`demand_signals` (frequency, views, velocity), `competition_signals` (counts,
average views, saturation), `novelty_rationale`, `confidence` (evidence volume
+ data completeness), `score`, and `recommended_angle`.

**Score** (0..100, deterministic):

```
score = 100 * (0.40*demand + 0.30*gap + 0.20*novelty + 0.10*confidence)
demand   = supporting total views / max single-video views (capped at 1)
gap      = 1 - min(avg supporting views / sample median, 1)
novelty  = 1 when the topic is classified underserved, else 0
confidence = evidence-volume × data-completeness factor
```

**Critical rule:** the system identifies opportunities — it does NOT rewrite
or imitate competitor videos. All opportunity text is generated from topic
terms and measured signals only; tests assert no competitor title or
description appears in any opportunity field. The output is suitable as input
to the future Research Engine.

## Quota / cost strategy

* **Only documented unit costs are counted** (`search`=100, `videos`=1,
  `channels`=1) — quota values are never invented; the API does not expose
  remaining quota, so a configurable **per-run budget** is enforced
  *before* each call (`YOUTUBE_QUOTA_BUDGET_UNITS_PER_RUN`, default 1000).
  The documented default daily quota (10,000/project) is configuration,
  labeled informational.
* **No unnecessary API calls:** search pages and video/channel details are
  cached (`discovery_cache` table, TTL) — re-runs and retries of completed
  lookups never spend quota; video ids are deduplicated within and across
  pages; result limits are configurable (`YOUTUBE_DISCOVERY_RESULT_LIMIT`).
* **Usage tracking:** every API call records provider, endpoint, latency,
  success/failure, and quota metadata (`provider_usage.metadata`) — never the
  API key.

## Limitations

1. **REAL YouTube API connectivity: NOT RUN — owner test pending.** The
   provider is verified against mocked HTTP (unit) and a realistic local
   simulation of the documented API (integration, `make test-simulated`);
   the live owner-run against `https://www.googleapis.com` has not been
   performed. `SIMULATED YOUTUBE TEST: PASS`.
2. Analysis and clustering are deterministic and interpretable, but not
   semantically deep — the `Clusterer` protocol is the seam for a future
   embedding/LLM enhancement.
3. The YouTube API does not expose remaining quota; the per-run budget is the
   quota guard (daily cross-run tracking is a future control-plane feature).
4. `search` relevance is the API's default ranking; deeper market signals
   (trends over time) need historical snapshots (future phases).

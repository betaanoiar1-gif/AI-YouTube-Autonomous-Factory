# Content Plane — Phase 3A

## Scope

Phase 3A adds the deterministic first content-design stage:

`opportunity_list + matching research_report → content_brief → narrative_outline`

This stage is offline and does not call an LLM, search provider, or paid API. It deliberately stops before full script writing.

## Inputs

Run `factory pipeline run --job-type content_brief` with a project id and a payload containing:

- `opportunity_list_artifact_id`: stored Phase 1 opportunity-list artifact.
- `opportunity_id`: selected opportunity id within that artifact.
- `research_report_artifact_id`: stored Phase 2 report for that same opportunity.
- Optional `target_audience`, `intended_tone`, and non-negative `estimated_duration_seconds`.

The engine rejects a report whose opportunity id or project does not match. When a report artifact id is omitted, the latest project research report is resolved and still checked against the selected opportunity.

Example:

```bash
python -m factory.cli pipeline run --job-type content_brief --project-id proj-1 \
  --input-artifact-id <opportunity-list-artifact-id> \
  --payload '{"opportunity_list_artifact_id":"<opportunity-list-artifact-id>","opportunity_id":"<opportunity-id>","research_report_artifact_id":"<research-report-artifact-id>"}'
```

## Outputs and lineage

- `content_brief`: audience, promise/question, originality angle, content gaps, intended tone, constraints, supported key points, unresolved claims to avoid, and source/evidence references.
- `narrative_outline`: hook, setup, escalation, turning point, resolution, and final insight beats.
- The job output is the `narrative_outline` artifact. The brief is stored as an intermediate artifact in the same job lineage.
- Both artifacts preserve input artifact ids. The outline also references the brief artifact in its source lineage.
- The artifact store validates payloads against the registered Pydantic contract; JSON Schema 2020-12 files are maintained alongside the models.

## Evidence and originality rules

- Only claims marked `SUPPORTED` or `MULTI_SOURCE_SUPPORTED` are eligible for factual key points.
- Claim evidence references are normalized to concrete evidence ids, including the Phase 2 representation that currently places source ids in `VerifiedClaim.evidence_refs`.
- Source ids and URLs are retained alongside evidence references.
- Unverified, contested, contradicted, insufficient-evidence claims, unresolved questions, and contradiction descriptions are listed as unresolved content to avoid presenting as facts.
- If no evidence can be traced for a claim, it is not promoted to a factual key point.
- The title and angle derive from the selected opportunity; competitor titles, descriptions, and transcripts are not copied.

## Reliability

The `CONTENT_BRIEF` job uses the existing job runner, checkpoint store, artifact store, retry behavior, and CLI facade. The brief is checkpointed before outline creation. A resumed job reuses the saved brief artifact rather than creating a duplicate. No database migration was needed.

## Boundaries

Phase 3A does not generate a full script, visual assets, voice, timeline, rendered video, QA report, or publishing package. Those remain later phases.

# Artifact system

**Principle:** every major pipeline stage produces a **structured, typed,
versioned artifact** — never unstructured text passed between modules.

## Concepts

An **artifact** is the canonical output of a pipeline stage:

```
ArtifactRecord:
  id: UUID
  type: ArtifactType            # discovery_result, analysis_result, ...
  schema_version: "1.0.0"       # semver contract version
  version: int                  # version within its lineage (1, 2, 3, ...)
  project_id, job_id            # logical references
  lineage_key: str | None       # groups versions of the same logical output
  storage_ref: str              # relative path inside the artifact root
  checksum: sha256              # verified on every read
  byte_size: int
  metadata: dict
  created_at
```

The payload lives in the artifact store (filesystem, JSON); the database row
is the index. `factory/storage/artifacts.py::ArtifactStore` implements it.

## Guarantees

1. **Typed contracts.** Payloads are validated against the registered
   contract before they are written. Contracts exist in two mirrored forms,
   kept in sync by tests:
   * pydantic models — `factory/schemas/artifacts.py` (runtime validation,
     `extra="forbid"` so payloads cannot smuggle unexpected fields);
   * JSON Schema 2020-12 files — `schemas/artifacts/*.schema.json`
     (published, language-agnostic contracts).
2. **Versioned.** Every save creates a new row with the next version number
   for its lineage (`project_id` + `type` + `lineage_key`; `lineage_key`
   defaults to the producing `job_id`). `schema_version` is bumped on breaking
   contract changes; old artifacts remain readable because they are never
   overwritten.
3. **Immutable.** Each version gets its own id and storage path
   (`<project>/<type>/<id>.v<version>.json`). Saving never overwrites an
   existing artifact. Writes are atomic (temp file + rename).
4. **Integrity-checked.** Each record stores a SHA-256 checksum, verified on
   every read (`ArtifactIntegrityError` on mismatch).
5. **Contained.** Storage references are validated against the configured
   root; absolute paths, `..` segments, and escapes are rejected
   (`PathTraversalError`) via `factory/security/paths.py`.

## Artifact types (one per major stage)

| Type | Stage | Key contents |
| --- | --- | --- |
| `discovery_result` | discovery | videos found for a query + metrics snapshots + quota used |
| `analysis_result` | analysis | aggregate metrics, top performers, content patterns |
| `opportunity_list` | opportunity | ranked opportunities: title, topic, score, rationale, content gap |
| `research_report` | research | sources, claims (support status, confidence), contradictions, summary |
| `content_brief` | content | title, angle, target audience, key points, gaps addressed, duration |
| `script` | content | scenes (narration, visual description) → shots (type, duration, asset requirements) |
| `storyboard` | visual planning | per-scene shot plan with visual prompts and asset hints |
| `production_timeline` | production | tracks (video/audio/voice/music/subtitle/graphic) with clips |
| `qa_report` | QA | per-category checks (factual/narrative/visual/audio/subtitle/technical), overall status |
| `publish_package` | publishing | title + options, description, tags, category, language, thumbnail concept, schedule |

## API

```python
store = ArtifactStore(session_factory, root_dir)

record = store.save(  # validate → version → write → index
    ArtifactType.CONTENT_BRIEF,
    project_id="proj-1",
    payload={...} or ContentBrief(...),  # dict or pydantic model
    job_id="job-9",  # lineage defaults to job_id
    lineage_key="...",  # optional explicit lineage
    metadata={...},
)

record, payload = store.load(record.id)  # checksum-verified read
record, payload = store.load_latest(  # newest of a lineage
    "proj-1", ArtifactType.CONTENT_BRIEF, lineage_key="job-9"
)
```

Invalid payloads raise `ArtifactValidationError` listing every issue.
Missing artifacts raise `ArtifactNotFoundError`.

## Relationship to jobs and the domain model

* A job's `input_artifact_id`/`output_artifact_id` reference artifacts
  (logical references — artifacts outlive jobs).
* `artifacts.job_id` records which job produced the artifact.
* Artifact-first domain entities (Script, Timeline, QAReport, ContentBrief,
  ...) are the artifact payloads themselves; see `docs/domain-model.md`.

## Validation entry points

* `factory.schemas.artifacts.validate_artifact_payload(type, payload)` —
  pydantic validation → normalized dict (used by the store).
* `factory.schemas.artifacts.load_json_schema(type, schemas_dir)` — loads the
  published JSON Schema.
* Tests cross-check both representations with valid and invalid samples for
  every artifact type (`tests/unit/test_schemas_artifacts.py`).

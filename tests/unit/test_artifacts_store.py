"""Artifact store tests: typed validation, versioning, immutability,
checksum integrity, path traversal protection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    PathTraversalError,
)
from factory.schemas.artifacts import ArtifactType, ContentBrief
from factory.storage.artifacts import ArtifactStore


def _brief_payload(**overrides) -> dict:
    payload = {
        "brief_id": "brief-1",
        "opportunity_id": "opp-1",
        "title": "The Forgotten Tunnel",
        "angle": "A history-first investigation",
        "target_audience": "US history enthusiasts",
        "key_points": ["point one", "point two"],
        "content_gaps_addressed": ["no primary sources"],
        "estimated_duration_seconds": 900,
    }
    payload.update(overrides)
    return payload


class TestSaveAndLoad:
    def test_roundtrip(self, artifact_store: ArtifactStore):
        record = artifact_store.save(ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload())
        assert record.version == 1
        assert record.type == "content_brief"
        assert record.schema_version == "1.0.0"
        assert record.checksum
        loaded_record, payload = artifact_store.load(record.id)
        assert loaded_record.id == record.id
        assert payload["title"] == "The Forgotten Tunnel"
        assert payload["schema_version"] == "1.0.0"

    def test_accepts_pydantic_model(self, artifact_store: ArtifactStore):
        model = ContentBrief(
            brief_id="b2",
            opportunity_id="o2",
            title="t",
            angle="a",
            target_audience="us",
            estimated_duration_seconds=60,
        )
        record = artifact_store.save(ArtifactType.CONTENT_BRIEF, "proj-1", model)
        _, payload = artifact_store.load(record.id)
        assert payload["brief_id"] == "b2"

    def test_invalid_payload_rejected(self, artifact_store: ArtifactStore):
        with pytest.raises(ArtifactValidationError) as exc_info:
            artifact_store.save(ArtifactType.CONTENT_BRIEF, "proj-1", {"brief_id": "x"})
        assert exc_info.value.artifact_type == "content_brief"
        assert exc_info.value.issues  # details listed

    def test_extra_fields_rejected(self, artifact_store: ArtifactStore):
        with pytest.raises(ArtifactValidationError):
            artifact_store.save(
                ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(sneaky="field")
            )

    def test_load_missing_raises(self, artifact_store: ArtifactStore):
        with pytest.raises(ArtifactNotFoundError):
            artifact_store.load("missing-id")


class TestVersioning:
    def test_versions_increment_per_lineage(self, artifact_store: ArtifactStore):
        first = artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(), lineage_key="job-1"
        )
        second = artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(title="v2"), lineage_key="job-1"
        )
        assert first.version == 1
        assert second.version == 2
        assert first.id != second.id
        # Both files exist — nothing overwritten.
        assert artifact_store.resolve_path(first.storage_ref).exists()
        assert artifact_store.resolve_path(second.storage_ref).exists()
        _, payload_v1 = artifact_store.load(first.id)
        _, payload_v2 = artifact_store.load(second.id)
        assert payload_v1["title"] == "The Forgotten Tunnel"
        assert payload_v2["title"] == "v2"

    def test_separate_lineages_start_at_one(self, artifact_store: ArtifactStore):
        a = artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(), lineage_key="job-a"
        )
        b = artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(), lineage_key="job-b"
        )
        assert a.version == 1
        assert b.version == 1

    def test_lineage_defaults_to_job_id(self, artifact_store: ArtifactStore):
        a = artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(), job_id="job-9"
        )
        b = artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(), job_id="job-9"
        )
        assert (a.version, b.version) == (1, 2)

    def test_load_latest(self, artifact_store: ArtifactStore):
        artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(title="old"), lineage_key="L"
        )
        artifact_store.save(
            ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload(title="new"), lineage_key="L"
        )
        record, payload = artifact_store.load_latest(
            "proj-1", ArtifactType.CONTENT_BRIEF, lineage_key="L"
        )
        assert record.version == 2
        assert payload["title"] == "new"


class TestIntegrityAndSafety:
    def test_checksum_detects_tampering(self, artifact_store: ArtifactStore):
        record = artifact_store.save(ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload())
        path = artifact_store.resolve_path(record.storage_ref)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["title"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ArtifactIntegrityError):
            artifact_store.load(record.id)

    def test_storage_ref_traversal_rejected(self, artifact_store: ArtifactStore, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            artifact_store.resolve_path("../../etc/passwd")
        with pytest.raises(PathTraversalError):
            artifact_store.resolve_path("/absolute/path.json")

    def test_files_written_inside_root(self, artifact_store: ArtifactStore, tmp_path: Path):
        record = artifact_store.save(ArtifactType.CONTENT_BRIEF, "proj-1", _brief_payload())
        path = artifact_store.resolve_path(record.storage_ref)
        assert artifact_store.root.resolve() in path.resolve().parents

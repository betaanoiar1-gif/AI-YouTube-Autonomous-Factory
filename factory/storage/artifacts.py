"""Artifact store: versioned, immutable, integrity-checked pipeline artifacts.

Guarantees:

* **Typed contracts** — payloads are validated against the registered pydantic
  contract (and mirrored JSON Schema) before they are written.
* **Versioned** — every save creates a new artifact row with the next version
  number for its lineage (project + type + lineage key). Saving never
  overwrites an existing artifact: each version gets its own id and storage
  path.
* **Immutable** — files are written atomically (temp file + rename) and each
  record stores a SHA-256 checksum that is verified on every read.
* **Contained** — storage references are validated against the configured
  root (path traversal protection via :func:`factory.security.paths.safe_join`).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from factory.errors import ArtifactIntegrityError, ArtifactNotFoundError
from factory.observability.logging import get_logger
from factory.schemas.artifacts import ARTIFACT_CONTRACTS, ArtifactType, validate_artifact_payload
from factory.security.paths import safe_filename, validate_storage_ref
from factory.storage.models import Artifact

logger = get_logger(__name__)


class ArtifactRecord(BaseModel):
    """Metadata for a stored artifact (never contains secrets)."""

    id: str
    type: str
    schema_version: str
    version: int
    project_id: str | None = None
    job_id: str | None = None
    lineage_key: str | None = None
    storage_ref: str
    checksum: str
    byte_size: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    """Filesystem-backed, database-indexed artifact store."""

    def __init__(self, session_factory: Callable[[], Session], root_dir: Path | str) -> None:
        self._session_factory = session_factory
        self._root = Path(root_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(
        self,
        artifact_type: ArtifactType | str,
        project_id: str,
        payload: dict[str, Any] | BaseModel,
        *,
        job_id: str | None = None,
        lineage_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        """Validate, version, and persist an artifact payload.

        Returns the artifact record. Never overwrites an existing artifact.
        """
        artifact_type = ArtifactType(artifact_type)
        contract = ARTIFACT_CONTRACTS[artifact_type]
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        normalized = validate_artifact_payload(artifact_type, payload)

        lineage = lineage_key or job_id
        with self._session_factory() as session:
            version = self._next_version(session, project_id, artifact_type, lineage)
            artifact_id = uuid.uuid4().hex
            storage_ref = self._build_storage_ref(project_id, artifact_type, artifact_id, version)
            row = Artifact(
                id=artifact_id,
                type=artifact_type.value,
                schema_version=contract.schema_version,
                version=version,
                project_id=project_id,
                job_id=job_id,
                lineage_key=lineage,
                storage_ref=storage_ref,
                checksum="",  # filled after write
                byte_size=0,
                meta=metadata or {},
            )
            session.add(row)
            session.flush()

            data = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            target = validate_storage_ref(self._root, storage_ref)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target.with_suffix(target.suffix + ".tmp")
            tmp_path.write_bytes(data)
            tmp_path.replace(target)

            row.checksum = _sha256(data)
            row.byte_size = len(data)
            session.commit()

            record = ArtifactRecord(
                id=row.id,
                type=row.type,
                schema_version=row.schema_version,
                version=row.version,
                project_id=row.project_id,
                job_id=row.job_id,
                lineage_key=row.lineage_key,
                storage_ref=row.storage_ref,
                checksum=row.checksum,
                byte_size=row.byte_size,
                metadata=row.meta,
                created_at=row.created_at,
            )
        logger.info(
            "artifact_saved",
            extra={
                "artifact_id": record.id,
                "artifact_type": record.type,
                "version": record.version,
                "project_id": record.project_id,
                "job_id": record.job_id,
                "byte_size": record.byte_size,
            },
        )
        return record

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_record(self, artifact_id: str) -> ArtifactRecord:
        with self._session_factory() as session:
            row = session.get(Artifact, artifact_id)
            if row is None:
                raise ArtifactNotFoundError(f"Artifact not found: {artifact_id}")
            return self._to_record(row)

    def load(self, artifact_id: str) -> tuple[ArtifactRecord, dict[str, Any]]:
        """Load an artifact payload, verifying its checksum."""
        record = self.get_record(artifact_id)
        path = validate_storage_ref(self._root, record.storage_ref)
        data = path.read_bytes()
        if _sha256(data) != record.checksum:
            raise ArtifactIntegrityError(
                f"Checksum mismatch for artifact {artifact_id}: stored content was modified"
            )
        payload = json.loads(data.decode("utf-8"))
        return record, payload

    def load_latest(
        self,
        project_id: str,
        artifact_type: ArtifactType | str,
        *,
        lineage_key: str | None = None,
    ) -> tuple[ArtifactRecord, dict[str, Any]]:
        """Load the newest artifact of a type for a project (optionally a lineage)."""
        artifact_type = ArtifactType(artifact_type)
        from sqlalchemy import select

        with self._session_factory() as session:
            stmt = (
                select(Artifact)
                .where(
                    Artifact.project_id == project_id,
                    Artifact.type == artifact_type.value,
                    Artifact.lineage_key == lineage_key,
                )
                .order_by(Artifact.version.desc(), Artifact.created_at.desc())
                .limit(1)
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                raise ArtifactNotFoundError(
                    f"No artifact of type {artifact_type.value!r} for project {project_id}"
                )
            record = self._to_record(row)
        return self.load(record.id)

    def resolve_path(self, storage_ref: str) -> Path:
        """Resolve a storage reference to an absolute path inside the root."""
        return validate_storage_ref(self._root, storage_ref)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_storage_ref(
        project_id: str, artifact_type: ArtifactType, artifact_id: str, version: int
    ) -> str:
        # Every component is validated as a safe single path segment.
        parts = [
            safe_filename(project_id),
            safe_filename(artifact_type.value),
            safe_filename(f"{artifact_id}.v{version}.json"),
        ]
        return "/".join(parts)

    @staticmethod
    def _next_version(
        session: Session, project_id: str, artifact_type: ArtifactType, lineage_key: str | None
    ) -> int:
        from sqlalchemy import func, select

        stmt = select(func.max(Artifact.version)).where(
            Artifact.project_id == project_id,
            Artifact.type == artifact_type.value,
            Artifact.lineage_key == lineage_key,
        )
        current = session.execute(stmt).scalar_one_or_none()
        return (current or 0) + 1

    @staticmethod
    def _to_record(row: Artifact) -> ArtifactRecord:
        return ArtifactRecord(
            id=row.id,
            type=row.type,
            schema_version=row.schema_version,
            version=row.version,
            project_id=row.project_id,
            job_id=row.job_id,
            lineage_key=row.lineage_key,
            storage_ref=row.storage_ref,
            checksum=row.checksum,
            byte_size=row.byte_size,
            metadata=row.meta,
            created_at=row.created_at,
        )

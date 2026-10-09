"""Typed artifact contracts (schemas) for pipeline outputs.

Every major pipeline stage produces a structured, versioned artifact — never
unstructured text passed between modules. Contracts exist in two mirrored
forms (kept in sync by tests):

* pydantic models in :mod:`factory.schemas.artifacts` (runtime validation);
* JSON Schema files in ``schemas/artifacts/*.schema.json`` (published
  contracts, language-agnostic).
"""

from factory.schemas.artifacts import (
    ARTIFACT_CONTRACTS,
    ArtifactType,
    validate_artifact_payload,
)

__all__ = [
    "ARTIFACT_CONTRACTS",
    "ArtifactType",
    "validate_artifact_payload",
]

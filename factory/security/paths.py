"""Path safety: containment checks and storage-reference validation.

The artifact store keeps every file inside a configured root directory.
``safe_join`` is the single choke point used to build storage paths; it
rejects absolute paths, ``..`` segments, and any resolved path that escapes
the root.
"""

from __future__ import annotations

import re
from pathlib import Path

from factory.errors import PathTraversalError

_SAFE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")


def safe_filename(name: str) -> str:
    """Validate a single path component (no separators, no traversal)."""
    if not _SAFE_FILENAME_PATTERN.match(name):
        raise PathTraversalError(f"Unsafe path component: {name!r}")
    return name


def safe_join(root: Path, *parts: str) -> Path:
    """Join ``parts`` onto ``root`` and verify the result stays inside ``root``.

    Raises :class:`PathTraversalError` if any part is absolute, contains a
    ``..`` segment, or the resolved result escapes the resolved root.
    """
    root_resolved = root.resolve()
    candidate = root_resolved
    for part in parts:
        if not part:
            raise PathTraversalError("Empty path component")
        if Path(part).is_absolute() or part.startswith("/") or part.startswith("\\"):
            raise PathTraversalError(f"Absolute path component not allowed: {part!r}")
        segments = Path(part).parts
        if any(segment in ("..",) for segment in segments):
            raise PathTraversalError(f"Path traversal segment not allowed: {part!r}")
        if "\\" in part or "\x00" in part:
            raise PathTraversalError(f"Unsafe characters in path component: {part!r}")
        candidate = candidate.joinpath(*segments)
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathTraversalError(f"Path escapes root {root_resolved}: {candidate}")
    return resolved


def validate_storage_ref(root: Path, storage_ref: str) -> Path:
    """Resolve a stored relative storage reference and verify containment."""
    if not storage_ref or storage_ref.startswith("/") or "\\" in storage_ref:
        raise PathTraversalError(f"Unsafe storage reference: {storage_ref!r}")
    return safe_join(root, *storage_ref.split("/"))

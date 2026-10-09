"""Controlled subprocess execution (foundation for later render/encode steps).

Rules enforced here and relied on by later phases (video rendering, asset
processing):

* commands are passed as argument lists — the shell is never used;
* the executable must be explicitly allow-listed (or resolve on ``PATH``);
* a hard timeout is always applied;
* the working directory, when given, must be inside the project workspace;
* errors are raised as :class:`UnsafeCommandError` / ``TimeoutExpired`` with
  output redacted by the caller's logging layer.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from factory.errors import UnsafeCommandError
from factory.security.paths import safe_join


def safe_run(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    workspace_root: Path | None = None,
    allowed_executables: set[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` under strict safety controls and return the result.

    Raises :class:`UnsafeCommandError` if the command fails validation or
    exits non-zero; ``subprocess.TimeoutExpired`` on timeout.
    """
    if not command or not all(isinstance(part, str) and part for part in command):
        raise UnsafeCommandError("Command must be a non-empty sequence of non-empty strings")

    executable = command[0]
    if allowed_executables is not None and executable not in allowed_executables:
        raise UnsafeCommandError(f"Executable is not allow-listed: {executable!r}")
    if shutil.which(executable) is None and not Path(executable).is_file():
        raise UnsafeCommandError(f"Executable not found: {executable!r}")

    run_cwd: Path | None = None
    if cwd is not None:
        if workspace_root is None:
            raise UnsafeCommandError("workspace_root is required when cwd is provided")
        if cwd.is_absolute():
            if not cwd.is_relative_to(workspace_root.resolve()):
                raise UnsafeCommandError(f"cwd escapes workspace root: {cwd}")
            run_cwd = cwd
        else:
            run_cwd = safe_join(workspace_root, *cwd.parts)

    try:
        result = subprocess.run(  # noqa: S603 - argument list, no shell, allow-listed above
            list(command),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=run_cwd,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnsafeCommandError(
            f"Command timed out after {timeout_seconds}s: {executable}"
        ) from exc
    if result.returncode != 0:
        raise UnsafeCommandError(
            f"Command exited with code {result.returncode}: {executable} "
            f"(stderr: {result.stderr.strip()[:500]})"
        )
    return result

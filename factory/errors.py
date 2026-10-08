"""Exception hierarchy for the factory.

All exceptions are designed to be safe to log and serialize: they must never
carry secrets (API keys, authorization headers, credentials). Messages are
sanitized by the observability layer before they reach any log sink.
"""

from __future__ import annotations

from typing import Any


class FactoryError(Exception):
    """Base class for all factory errors."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationError(FactoryError):
    """Configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class SecurityError(FactoryError):
    """Base class for security violations."""


class PathTraversalError(SecurityError):
    """A path escaped its allowed root directory."""


class UnsafeURLError(SecurityError):
    """A URL failed safety validation (scheme, host, embedded credentials)."""


class UnsafeCommandError(SecurityError):
    """A subprocess invocation failed safety validation."""


# ---------------------------------------------------------------------------
# Providers (LLM and friends)
# ---------------------------------------------------------------------------


class ProviderError(FactoryError):
    """Base class for provider errors.

    Carries only non-secret metadata: provider name, model, HTTP status,
    provider error code, and a sanitized message.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.error_code = error_code


class ProviderConfigurationError(ProviderError):
    """Provider is not configured correctly (e.g. missing API key)."""


class AuthenticationError(ProviderError):
    """Authentication failed (HTTP 401). The key is missing, malformed, or revoked."""


class ScopeError(ProviderError):
    """The credential lacks the required scope (HTTP 403)."""


class InsufficientFundsError(ProviderError):
    """Provider balance and plan allowance are exhausted (HTTP 402)."""


class ModelNotFoundError(ProviderError):
    """The requested model does not exist or is unavailable (HTTP 404)."""


class ProviderValidationError(ProviderError):
    """The provider rejected the request body (HTTP 422)."""


class RateLimitError(ProviderError):
    """Rate limit exceeded (HTTP 429). Carries the server-provided retry delay."""

    def __init__(
        self, message: str, *, retry_after_seconds: float | None = None, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class ProviderHTTPError(ProviderError):
    """The provider returned an unexpected HTTP error."""


class ProviderTimeoutError(ProviderError):
    """The provider request timed out."""


class ProviderUnavailableError(ProviderError):
    """The provider endpoint could not be reached."""


class MalformedResponseError(ProviderError):
    """The provider response could not be parsed or failed schema checks."""


class StructuredOutputError(ProviderError):
    """The provider failed to produce output matching the required schema."""


class BudgetExceededError(ProviderError):
    """A configured request/token budget was exceeded."""


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobError(FactoryError):
    """Base class for job system errors."""


class InvalidJobTransitionError(JobError):
    """A job status transition is not allowed."""

    def __init__(self, job_id: str, from_status: str, to_status: str) -> None:
        super().__init__(
            f"Invalid job transition for job {job_id}: {from_status} -> {to_status}",
            details={"job_id": job_id, "from_status": from_status, "to_status": to_status},
        )
        self.job_id = job_id
        self.from_status = from_status
        self.to_status = to_status


class JobNotFoundError(JobError):
    """A job does not exist."""


class JobCancelledError(JobError):
    """A job was cancelled while running."""


# ---------------------------------------------------------------------------
# Artifacts & schemas
# ---------------------------------------------------------------------------


class ArtifactError(FactoryError):
    """Base class for artifact system errors."""


class ArtifactValidationError(ArtifactError):
    """An artifact payload failed contract validation."""

    def __init__(self, artifact_type: str, issues: list[str]) -> None:
        super().__init__(
            f"Artifact payload failed validation for type '{artifact_type}': " + "; ".join(issues),
            details={"artifact_type": artifact_type, "issues": issues},
        )
        self.artifact_type = artifact_type
        self.issues = issues


class ArtifactNotFoundError(ArtifactError):
    """An artifact does not exist."""


class ArtifactIntegrityError(ArtifactError):
    """An artifact's checksum does not match its stored content."""

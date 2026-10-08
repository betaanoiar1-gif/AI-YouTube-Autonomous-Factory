"""Normalized internal request/response model for LLM calls.

The internal system never depends on CleanAPIs-specific (or any other
provider's) request/response formats. Providers translate between these
normalized models and their wire formats.

Only fields actually supported by the verified provider surface are modeled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

MessageRole = Literal["system", "user", "assistant", "tool"]


class LLMMessage(BaseModel):
    """A single chat message."""

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class LLMRequest(BaseModel):
    """A normalized LLM completion request.

    ``model`` may be ``None``, in which case the provider default is used.
    ``metadata`` carries caller context (e.g. ``purpose``); it is used for
    usage tracking and logging, never sent to the provider.
    """

    model: str | None = None
    messages: list[LLMMessage] = Field(min_length=1)
    system: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stop: list[str] | None = None
    seed: int | None = None
    json_mode: bool = False
    use_cache: bool = True
    cache_ttl_seconds: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def effective_messages(self) -> list[LLMMessage]:
        """Return messages with the optional system preamble prepended."""
        if self.system:
            return [LLMMessage(role="system", content=self.system), *self.messages]
        return list(self.messages)


class LLMUsage(BaseModel):
    """Token usage for a request. Fields are ``None`` when the provider did
    not report them — we never silently estimate."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"

    @classmethod
    def from_provider(cls, value: str | None) -> FinishReason:
        if value is None:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class LLMResponse(BaseModel):
    """A normalized LLM completion response."""

    content: str
    provider: str
    model: str
    usage: LLMUsage = Field(default_factory=LLMUsage)
    finish_reason: FinishReason = FinishReason.UNKNOWN
    latency_ms: int
    request_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cached: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

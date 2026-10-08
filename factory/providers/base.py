"""Provider interfaces (contracts).

CleanAPIs is the real implementation of :class:`LLMProvider` in Phase 0.
All other providers are contracts only — they define the shape Phase 1+
implementations must satisfy. They are NOT fake implementations: calling an
unimplemented provider raises ``NotImplementedError`` with a pointer to the
phase that will implement it.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from factory.errors import StructuredOutputError
from factory.providers.llm.types import LLMMessage, LLMRequest, LLMResponse

T = TypeVar("T", bound=BaseModel)


class LLMProvider(ABC):
    """Large language model provider contract."""

    #: Stable provider identifier recorded in usage tracking (e.g. "cleanapis").
    name: str = "llm"

    @abstractmethod
    def complete(
        self,
        request: LLMRequest,
        *,
        job_id: str | None = None,
        purpose: str | None = None,
    ) -> LLMResponse:
        """Run a completion request and return the normalized response."""

    def complete_structured(
        self,
        request: LLMRequest,
        schema: type[T],
        *,
        job_id: str | None = None,
        purpose: str | None = None,
        max_correction_retries: int = 1,
    ) -> T:
        """Run a completion and return output validated against ``schema``.

        Default implementation (provider-agnostic): request JSON, parse it
        strictly, validate against the pydantic schema, and on failure retry
        once with a constrained correction request that includes the parse or
        validation error. Malformed output is never silently accepted.
        """
        structured_request = request.model_copy(update={"json_mode": True})
        response = self.complete(structured_request, job_id=job_id, purpose=purpose)
        try:
            return _parse_and_validate(response.content, schema)
        except StructuredOutputError as first_error:
            if max_correction_retries <= 0:
                raise
            correction = structured_request.model_copy(
                update={
                    "messages": [
                        *structured_request.messages,
                        LLMMessage(role="assistant", content=response.content),
                        LLMMessage(
                            role="user",
                            content=(
                                "Your previous response was invalid and was rejected. "
                                f"Reason: {first_error.message} "
                                "Return ONLY a JSON object that conforms to the required "
                                f"schema ({schema.__name__}). No prose, no markdown fences."
                            ),
                        ),
                    ]
                }
            )
            corrected = self.complete(correction, job_id=job_id, purpose=purpose)
            return _parse_and_validate(corrected.content, schema)


def _parse_and_validate(content: str, schema: type[T]) -> T:
    """Strictly parse JSON content and validate it against ``schema``."""
    text = content.strip()
    # Tolerate a single pair of markdown code fences, nothing else.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"Model output is not valid JSON: {exc.msg} (at line {exc.lineno} column {exc.colno})",
            details={"schema": schema.__name__},
        ) from exc
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        issues = [
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        ]
        raise StructuredOutputError(
            f"Model output failed schema validation ({schema.__name__}): " + "; ".join(issues),
            details={"schema": schema.__name__, "issues": issues},
        ) from exc


class ASRProvider(ABC):
    """Speech-to-text provider contract (Phase: production plane)."""

    name: str = "asr"

    @abstractmethod
    def transcribe(self, audio_ref: str, *, language: str | None = None) -> str:
        """Transcribe an audio asset reference to text."""
        raise NotImplementedError("ASRProvider is a Phase 0 contract; implementation comes later")


class VisionProvider(ABC):
    """Vision/image understanding provider contract (Phase: quality plane)."""

    name: str = "vision"

    @abstractmethod
    def analyze(self, image_ref: str, prompt: str) -> str:
        """Analyze an image asset with a text prompt."""
        raise NotImplementedError(
            "VisionProvider is a Phase 0 contract; implementation comes later"
        )


class EmbeddingProvider(ABC):
    """Embedding provider contract (Phase: intelligence plane clustering)."""

    name: str = "embedding"

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        raise NotImplementedError(
            "EmbeddingProvider is a Phase 0 contract; implementation comes later"
        )


class ImageProvider(ABC):
    """Image generation provider contract (Phase: production plane)."""

    name: str = "image"

    @abstractmethod
    def generate(self, prompt: str, *, job_id: str | None = None) -> str:
        """Generate an image; returns an asset reference."""
        raise NotImplementedError("ImageProvider is a Phase 0 contract; implementation comes later")


class VideoProvider(ABC):
    """Video rendering provider contract (Phase: production plane)."""

    name: str = "video"

    @abstractmethod
    def render(self, timeline_ref: str, *, job_id: str | None = None) -> str:
        """Render a production timeline; returns a video asset reference."""
        raise NotImplementedError("VideoProvider is a Phase 0 contract; implementation comes later")


class TTSProvider(ABC):
    """Text-to-speech provider contract (Phase: production plane)."""

    name: str = "tts"

    @abstractmethod
    def synthesize(self, text: str, *, voice: str, job_id: str | None = None) -> str:
        """Synthesize speech; returns an audio asset reference."""
        raise NotImplementedError("TTSProvider is a Phase 0 contract; implementation comes later")


class DiscoveryProvider(ABC):
    """Content discovery provider contract (Phase 1: intelligence plane).

    Implementations must use documented, authorized data APIs (e.g. the
    YouTube Data API v3), respect quotas and rate limits, and expose quota
    consumption. Scraping, undocumented endpoints, and quota evasion are
    explicitly out of scope for this project.
    """

    name: str = "discovery"

    @abstractmethod
    def discover_videos(
        self,
        query: str,
        *,
        max_results: int = 50,
        published_after: str | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Discover videos for a search query. Returns raw candidate dicts."""
        raise NotImplementedError(
            "DiscoveryProvider is a Phase 0 contract; the YouTube implementation "
            "is built in Phase 1"
        )

    @abstractmethod
    def get_video_metrics(
        self, video_ids: list[str], *, job_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch performance metrics for the given videos."""
        raise NotImplementedError(
            "DiscoveryProvider is a Phase 0 contract; the YouTube implementation "
            "is built in Phase 1"
        )

    @abstractmethod
    def get_channel_metrics(self, channel_id: str, *, job_id: str | None = None) -> dict[str, Any]:
        """Fetch performance metrics for a channel."""
        raise NotImplementedError(
            "DiscoveryProvider is a Phase 0 contract; the YouTube implementation "
            "is built in Phase 1"
        )

    @abstractmethod
    def quota_used(self) -> int | None:
        """Return quota units consumed so far, or ``None`` when unknown."""
        raise NotImplementedError(
            "DiscoveryProvider is a Phase 0 contract; the YouTube implementation "
            "is built in Phase 1"
        )

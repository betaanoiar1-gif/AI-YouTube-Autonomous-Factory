"""Provider abstraction layer.

Business logic depends on the interfaces in :mod:`factory.providers.base`
(``LLMProvider``, ``ASRProvider``, ``VisionProvider``, ``EmbeddingProvider``,
``ImageProvider``, ``VideoProvider``, ``TTSProvider``, ``DiscoveryProvider``),
never on a concrete provider. CleanAPIs is the real implementation of
``LLMProvider`` in Phase 0 (``factory.providers.cleanapis``); the remaining
providers are contracts only, to be implemented in later phases.
"""

from factory.providers.base import (
    ASRProvider,
    DiscoveryProvider,
    EmbeddingProvider,
    ImageProvider,
    LLMProvider,
    TTSProvider,
    VideoProvider,
    VisionProvider,
)
from factory.providers.llm.types import (
    FinishReason,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)

__all__ = [
    "ASRProvider",
    "DiscoveryProvider",
    "EmbeddingProvider",
    "FinishReason",
    "ImageProvider",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "TTSProvider",
    "VideoProvider",
    "VisionProvider",
]
